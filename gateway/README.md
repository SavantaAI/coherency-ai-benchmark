# Gateway

The gateway is a reverse proxy for model API calls. It runs in its own
container and is the only place the real API key exists. The agent's
container joins an internal Docker network where the gateway is its only
peer. It holds a placeholder key and has no other route out.

The code is Python 3.12 with the standard library, plus the `openssl` command
for certificates.

## Modes

**Transparent mode is the intended mode** (decision D-007). The agent's
container resolves `api.anthropic.com` to the gateway (an `--add-host` entry)
and trusts a private CA the gateway creates at start. The gateway answers TLS
for that name. Each harness keeps its default base URL.
[Why transparent mode](#why-transparent-mode) shows what changes otherwise.

**Path-prefix mode** (`http://<gateway>:8080/anthropic/...`) is off by default
and for debugging only. It needs a base URL setting in each harness, and both
tested harnesses change their requests when the host is not the first-party
one. The plain listener may bind a routable address only with an explicit
opt-in (`listen.prefix_routes` or `GATEWAY_PREFIX_ROUTES=1`).

Without that opt-in, the plain listener binds `127.0.0.1:8080` and answers
only `GET /healthz`, which reports `{"status":"ok","routes":{...}}` and
nothing about traffic. The image's health check uses it.

**One provider per gateway, enforced.** With `single_provider` on (the
default), startup fails with exit code 2 unless exactly one route is active.
A route is active only when its key file is present. An inactive route has no
certificate, no path prefix and no health entry, so the agent cannot reach
another provider through the gateway.

## What it does with a request

1. **Head.** The gateway reads the request line and headers itself. If the
   connection ends, or the read deadline passes, before the blank line that
   ends the head, the request is logged as `incomplete_request` or
   `client_read_timeout` and no upstream connection is opened. Lines must end
   in CRLF. A bare CR or any other control byte except a tab inside a header
   value, anywhere in the request line or a header line, gets 400, as does
   obsolete line folding. More than 100 headers or 64 KiB of headers get 431.
   The gateway builds the header list from the (name, value) pairs it parsed
   itself, and trims only spaces and tabs around values (not `\xa0` or
   `\x85`). Versions other than HTTP/1.1 and HTTP/1.0 get 505. An
   unexpected error while handling a request is always logged
   (`internal_error`). Before the response head has been sent, the client
   gets a 500. After it, the connection is cut, since a second status cannot
   be sent.
2. **Target.** The request target must be an origin-form path
   (`/v1/messages?...`). It refuses with 400 and closes the connection for an
   absolute URL, `*`, a fragment, any `%` in the path, a backslash, an empty
   segment (including a leading `//` or a trailing `/`), and a `.` or `..`
   segment. The query string may hold any visible ASCII and is forwarded byte
   for byte, an empty `?` included.
3. **Framing.** It refuses with 400 and closes the connection for:
   `Transfer-Encoding` together with `Content-Length`; more than one
   `Content-Length`, or one that is not plain digits; any `Transfer-Encoding`
   other than a single `chunked`; a chunk size that is not plain hex; a chunk
   whose length or CRLF terminator does not match; and trailers over 8 KiB.
   Spaces or tabs before a chunk extension (`2 ;ext`) are allowed.
   `Expect` other than `100-continue` gets 417. A declared `Content-Length`
   is checked against `max_request_bytes` (413) and the buffer budget (503)
   before any `100 Continue` is sent. The body is read whole and goes upstream
   with a `Content-Length`. Request trailers are dropped.

   After any refusal the gateway sends the response, shuts down its write
   side, and discards incoming bytes before closing, so a client still
   sending a body can read the refusal instead of a connection reset. The
   drain is best effort and bounded: it stops at the client's end of stream,
   after 2 seconds, or after 64 MiB, whichever comes first. A client that
   keeps sending past those bounds may still see a reset. This holds over
   TLS too.
4. **Host.** There must be exactly one `Host` header with a valid hostname
   and, if a port is given, 1 to 5 ASCII digits in the range 1 to 65535
   (otherwise 400). In transparent mode it must name the TLS server name
   (SNI), and its port, explicit or the default 443, must be the listener's.
   Otherwise 421.
5. **Route and allowlist.** In transparent mode the SNI picks the route; a
   name that is not an active route's host fails the TLS handshake with an
   `unrecognized_name` alert. Then the (method, path) must be on the route's
   allowlist, or the request gets 403. Unknown paths in path-prefix mode get
   404. Every refusal is logged.
6. **Request headers.** The gateway drops any `x-api-key` and `authorization`
   the client sent and adds the route's credential. It sets `Host` to the
   upstream host. Hop-by-hop headers (RFC 9110 section 7.6.1, plus `expect`)
   are not forwarded. **`Accept-Encoding` is the one request header besides
   the credential that the gateway changes: the upstream is always asked for
   `identity`**, whatever the client offered. The client's value is logged as
   `client_accept_encoding` (`(none)` if it sent none). Every other header and
   the body pass unchanged.
7. **Response head.** Interim 1xx responses other than 100 (for example 103
   Early Hints) are skipped and logged as `interim_statuses`. The gateway
   answers 502 itself, relaying nothing of the upstream response, when:
   - the status reason, a header name or a header value contains a loaded key
     (`credential_in_response_head`, with an alert on stderr);
   - the response has a body and a `Content-Encoding` other than `identity`
     (`upstream_encoding_not_screenable`). Bodyless responses (to HEAD, 204,
     304) pass whatever their `Content-Encoding` says;
   - `Transfer-Encoding` is anything but absent or a single `chunked`
     (optional whitespace around it allowed), or `Content-Length` values
     conflict (`upstream_bad_framing`).
8. **Response body.** The gateway reads the body itself: Python's
   http.client parses only the status line and headers, and one framing
   decision (above) drives both validation and decoding. Chunks are decoded
   strictly: a bad size line, a chunk-data delimiter other than CRLF, a
   missing final CRLF after the zero chunk, or an early end is a truncation.
   See [Screening](#screening). Status, headers and body bytes that pass come
   back unchanged. A fixed length is relayed as `Content-Length`. Chunked
   bodies stay chunked; close-delimited bodies go out chunked. Response
   trailers are dropped.

   Failures before the client has received a head (any buffered body)
   become an error response, logged with that status: 502
   `upstream_truncated` for a truncation, 504 `upstream_timeout` for a
   stalled read. Once an event stream has started, the connection is cut and
   the log names the failure in `upstream_error`.
9. **Time.** There is no limit on total response time. The upstream may stay
   silent for `upstream_timeout_seconds` (900) between bytes. The TCP connect
   and TLS handshake to the upstream must finish within
   `upstream_connect_timeout_seconds` (15), counted from after name
   resolution.
10. **Disconnects.** A watcher thread aborts the upstream connection within
    250 ms of the client closing or half-closing its side, at any stage:
    during the upstream connect and TLS handshake, while the upstream is still
    thinking, and while a response streams. On Linux this uses `POLLRDHUP` and
    works for TLS. A client that half-closes (`shutdown(SHUT_WR)`) before
    reading therefore loses its response (D-008). Neither harness nor any
    common HTTP client does that.
11. **Upstream TLS.** Upstream connections use the system trust store with
    normal certificate and hostname checks. Upstreams must be `https`. Plain
    `http` needs `insecure_allow_http_upstream: true`, which exists for tests.

Errors the gateway writes itself use neutral wording and the provider's error
shape: `{"type":"error","error":{"type":"not_found_error","message":"Not found"}}`
for Anthropic, `{"error":{"message":"Not found","type":"invalid_request_error","param":null,"code":null}}`
for OpenAI. Responses the gateway writes itself carry no `Server` header.

## Screening

The gateway relays a response only in units it has screened, so a loaded key
never reaches the client in the forms below.

- **Units.** For a `text/event-stream` response, a unit is one whole
  server-sent event: the bytes up to and including the empty line that ends
  it, with lines ending in CRLF, LF or CR. The gateway holds an event's bytes
  until its end arrives, screens it, and only then sends it. Events therefore
  reach the client one whole event at a time, as soon as each one ends. Bytes
  after the last empty line when the stream ends form a final unit, screened
  and relayed like the others. Any other body is one unit: the gateway
  buffers it whole, screens it, and then sends the head and the body.
- **Forms.** The scan uses no JSON parser. A unit is refused if a key
  appears in its raw bytes, or in the same bytes with JSON string escapes
  undone textually: `\uXXXX` (surrogate pairs combined) and `\"`, `\\`,
  `\/`, `\b`, `\f`, `\n`, `\r`, `\t`. For an event, both scans also run on
  its `data:` lines joined with newlines, as an SSE client joins them, after
  stripping one leading UTF-8 byte order mark. The bytes on the wire are not
  changed. The same check runs for every status code and every route. Usage
  parsing has no part in it.
- **On a hit.** Nothing of the unit is sent, and the gateway prints an alert
  and logs `credential_in_response`. For a buffered body the client gets a
  502 and nothing of the upstream response. For an event stream, the events
  before it have gone out; the connection is cut.
- **Caps.** A buffered body over `max_response_body_bytes` (16 MiB) cannot be
  screened: the client gets 502 (`response_too_large`). An event over
  `max_sse_event_bytes` (16 MiB) cannot either: the connection is cut
  (`sse_event_too_large`), after every complete event before it was relayed.
  Model API responses are far below both.
- **Not covered.** A key encoded some other way (base64, percent-encoding,
  split across two events, or escaped twice) is not detected.
- **After screening.** Usage parsing and the body store see a unit only after
  it passed and was sent. Usage counts only events a client dispatches: a
  final unit without its closing empty line is relayed but not counted.

A stream whose events end in a bare CR waits for the next byte before the
event is sent, because the CR may be the first half of a CRLF; at the end of
the stream a held CR counts as a line break. Anthropic and OpenAI end events
with LF.

## Allowlists

The shipped [config.json](config.json) allows only these:

| Route | Method | Path |
|---|---|---|
| anthropic | POST | `/v1/messages` (any query) |
| anthropic | POST | `/v1/messages/count_tokens` |
| anthropic | GET | `/api/claude_code/settings` |
| anthropic | GET | `/api/claude_code/policy_limits` |
| openai | POST | `/v1/chat/completions` |
| openai | POST | `/v1/responses` (create) |
| openai | GET, DELETE | `/v1/responses/{id}` (retrieve, delete) |
| openai | POST | `/v1/responses/{id}/cancel` |
| openai | GET | `/v1/responses/{id}/input_items` |

`{id}` matches one path segment of letters, digits, `_` or `-`, up to 128
characters. Every entry is an exact match otherwise. A `prefix` match also
exists in the config format for tests and local experiments; the shipped
config does not use it.

Claude Code 2.1.330 calls the two `/api/claude_code/*` paths before each turn
in transparent mode. Pi 1.6.0 calls only `/v1/messages`. One-shot turns and
tool-using turns of each harness needed nothing else (2026-11-21). The OpenAI
list follows OpenAI's public chat completions and Responses API reference and
has not been checked against a real client.

## Certificates

At every start the gateway runs `openssl` to create, in `/run/gateway-tls`
(mode 0700, owned by the gateway user):

- a P-256 CA named `ca_name` (default "Local Root CA"), valid for `cert_days`
  (7). The certificate is written as `ca_cert_file` (default `local-ca.crt`)
  and the key as `ca.key` (0600);
- one leaf certificate per active host, signed by that CA, naming only that
  host. The gateway picks the leaf by SNI.

Nothing goes to the log volume or the image. Only the CA certificate should
leave the container. A restarted gateway has a new CA, so copy the
certificate again after every start.

## Configuration

Top-level fields, with defaults:

| Field | Default | Meaning |
|---|---|---|
| `listen.host`, `listen.port` | `127.0.0.1`, 8080 | Plain listener |
| `listen.prefix_routes` | false | Serve path-prefix routes there (opt-in) |
| `transparent.enabled`, `.host`, `.port` | true, `0.0.0.0`, 443 | TLS listener |
| `transparent.tls_dir`, `.cert_days`, `.ca_name`, `.ca_cert_file` | see above | Certificates |
| `single_provider` | true | Startup needs exactly one active route |
| `log_dir` | `/var/log/gateway` | Request log and bodies |
| `store_bodies` | false | Store request and response bodies |
| `upstream_timeout_seconds` | 900 | Upstream silence allowed |
| `upstream_connect_timeout_seconds` | 15 | Upstream TCP connect plus TLS handshake |
| `client_idle_timeout_seconds` | 60 | Client silence allowed, between and within requests |
| `client_read_timeout_seconds` | 300 | A request's head and body must arrive within this of its first byte |
| `max_request_bytes` | 32 MiB | Request body cap (413) |
| `max_response_body_bytes` | 16 MiB | Buffered response body cap (502) |
| `max_sse_event_bytes` | 16 MiB | One event's cap (connection cut) |
| `max_stored_body_bytes` | 16 MiB | One stored body's cap (marker instead) |
| `max_buffered_bytes` | 512 MiB | Request and response buffers across all connections (503 beyond) |
| `max_connections` | 256 | Concurrent client connections |
| `insecure_allow_http_upstream` | false | Allow `http` upstreams (tests only) |

An idle kept-alive connection closes after `client_idle_timeout_seconds` (60)
without a byte. Once a request's first byte arrives, the rest of its head and
body must arrive within `client_read_timeout_seconds` (300), or the
connection is cut and the request is logged as `client_read_timeout`.

Memory is bounded per request by the caps above and across connections by
`max_buffered_bytes`. The budget covers exactly these allocations:

- a request body, for its declared length from before it is read (chunked:
  as each chunk arrives) until it has been sent upstream;
- a buffered response body, as it arrives, until the request ends;
- for an event stream, every byte read from the upstream, charged on
  arrival: the incomplete tail and any complete events still queued behind
  the one being written. Each event's bytes are released once its
  downstream write (and the usage and store copies) are done.

A request that would exceed it gets 503 (`buffer_limit`) before anything was
sent, or has its stream cut after the events already relayed. Because a read
(up to 64 KiB) is charged whole on arrival, whether an event just before a
refused one still went out can depend on how the upstream's bytes were split
into reads; the refused event itself never goes out. The budget does not
cover the fixed per-connection buffers (the socket file buffers, the read
buffer before it is charged, and a thread per connection plus a watcher
thread while a request is in flight).

Route fields are `name`, `prefix`, `upstream` (https, no user information,
query or fragment), `hosts`, `credential` (`header`, `format` containing
`{key}`, `key_file`), `usage` (`anthropic`, `openai` or `none`) and `allow` (a
non-empty list of `{method, path, match}`).

Values are type-checked: a boolean must be a JSON boolean, numbers are
range-checked, and hostnames, header names and paths are validated. Errors
never repeat an upstream URL, since it could hold a credential. Any error,
including an unreadable config file, stops the gateway with exit code 2 and a
message on stderr.

Environment variables override the file: `GATEWAY_CONFIG`,
`GATEWAY_PREFIX_ROUTES`, `GATEWAY_HOST`, `GATEWAY_PORT`, `GATEWAY_LOG_DIR`,
`GATEWAY_STORE_BODIES`, `GATEWAY_UPSTREAM_TIMEOUT`, `GATEWAY_TRANSPARENT`,
`GATEWAY_TLS_PORT`, `GATEWAY_TLS_DIR` and `GATEWAY_KEY_FILE_<NAME>` (for
example `GATEWAY_KEY_FILE_ANTHROPIC`). Booleans take `1` or `0`.

## Gateway usage is not a scorer input

Decision D-008: the scorer never reads `requests.jsonl` or anything derived
from it. The log records paths, hosts, sizes and each provider's usage object,
and even per-call token counts alone tell the harnesses apart (about 18,000
cached input tokens per call for Claude Code against about 2,300 for Pi on
the same model). The runner reads gateway usage for budgets, and the report
reads it for cost.

```sh
python -m gateway summary runs/<run>/gateway/requests.jsonl            # one line per inference call
python -m gateway summary --episode runs/<run>/gateway/requests.jsonl  # one line of totals
```

An inference call is a request to an inference endpoint whose outcome is a
2xx response relayed in full (`ok`) or unknown (`unknown`: the request was
written upstream but its response was not relayed in full, for example after
a client cancel, an upstream stall, a cut stream, or a 2xx the gateway
refused to relay, so the upstream may have generated output). A request that
never reached the upstream (a failed connect or TLS handshake, or a
disconnect during it) is not a call. Usage parsed from an incomplete relay is
marked partial. Error responses on inference endpoints are not
inference calls; the episode line counts them by status class. Records written
before `route_path` existed are matched on the upstream path without its
query.

Per-call lines hold `index`, `start`, `duration_ms`, `input_tokens`
(uncached), `cached_input_tokens`, `cache_write_tokens`, `output_tokens`,
`usage_status`, `outcome`, `inference` and `server_tool_use`, and no path,
route, host, size, header or file name. Only inference calls are listed
unless `--all` is given. The episode line holds `calls`, the total of each
token field (null if no call reported it), `calls_not_complete`,
`calls_outcome_unknown`, the total `duration_ms`, whether any call reported
server-side tool use, the worst `usage_status`, and `errors` (`4xx`, `5xx`,
`other`). Totals undercount by whatever incomplete calls did not report.

`server_tool_use` is true when Anthropic's `usage.server_tool_use` reports a
server-side tool, or when an OpenAI Responses output item or stream event is a
`web_search_call`, `file_search_call`, `code_interpreter_call`,
`image_generation_call`, `mcp_call` or `mcp_list_tools`. The OpenAI detection follows the public
API reference and is untested against the live API.

## The request log

`<log_dir>/requests.jsonl` gets one JSON object per request (mode 0600).
Headers are not in it; with `store_bodies` on they go to the meta files.

| Field | Meaning |
|---|---|
| `time`, `id` | When the request's first byte arrived (UTC), request id |
| `duration_ms` | From the request's first byte to the last successful write back to the client |
| `ttfb_ms`, `first_body_ms` | To the upstream response head; to the first body byte sent back |
| `mode`, `route`, `usage_format`, `host` | Listener role, route, usage shape, SNI |
| `method`, `path`, `route_path`, `upstream_path` | Raw target, route-relative path, where it went |
| `status`, `upstream_status` | Status sent back; the upstream's, when the gateway replaced it |
| `request_bytes`, `response_bytes` | Body bytes from the client and to it |
| `model`, `request_stream`, `stream`, `response_model` | From the bodies; `model` and `request_stream` come from a bounded scan of the request's top-level object (see below) |
| `inference`, `tokens`, `usage`, `usage_status`, `usage_error`, `server_tool_use` | See below |
| `client_accept_encoding`, `content_encoding`, `interim_statuses` | Encoding and framing notes |
| `upstream_sent` | False until the request is written upstream, then true: from then on the upstream may have generated output |
| `relay_complete` | The whole response was relayed to the client; a 2xx status alone does not say that |
| `request_body_withheld`, `response_body_withheld` | Why a stored body is the marker (see below) |
| `client_disconnected`, `gateway_error`, `upstream_error` | What went wrong |

The request's `model` and `stream` are found without parsing the body: a scan
walks the top-level object, skipping nested values by bracket depth, and
stops when both are found or after 1 MiB of the body, however large the body
is. A field not reached within that bound is logged as null. Both harnesses
send `model` first.

Usage is expected only on inference endpoints (`inference` names which:
`anthropic_messages`, `openai_chat` or `openai_responses`), on a 2xx response
with a body. Required counters:

| Inference | Required |
|---|---|
| `anthropic_messages` | `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens` |
| `openai_chat` | `prompt_tokens`, `prompt_tokens_details.cached_tokens`, `completion_tokens` |
| `openai_responses` | `input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens` |

Each must be a non-negative integer. `tokens` holds them normalised; one that
is missing or invalid stays null, never zero. `usage_status` is `complete`
when the terminal event arrived (`message_stop` for Anthropic; `[DONE]` for
OpenAI chat completions; `response.completed`, `response.incomplete` or
`response.failed` for the Responses API; or a whole non-streaming body),
every required counter is present and the response finished. A counter supplied as null or with a
wrong type in a later event replaces the earlier value and so counts as
missing. It is `partial` when usage was seen but
one of those failed, and `unavailable` when there is none or none was
expected. `usage_error` gives the reason and is set only where usage was
expected. OpenAI chat completions report streaming usage only when the caller
sets `stream_options.include_usage`; the gateway does not rewrite requests to
ask for it.

## Stored bodies and redaction

With `store_bodies` on, each forwarded request writes, under
`<log_dir>/bodies/` (mode 0700, files 0600):

- `<id>.request`, the request body, decoded from gzip or deflate;
- `<id>.response`, the response body, from screened units only;
- `<id>.meta.json`, the headers sent upstream (credential shown as
  `[credential]`), the response headers and the outcome.

All three are written for every forwarded request, including failed and
refused ones, before the log line. A body is written to a temporary file and
published by rename only when it completed. Otherwise the published file is
the fixed text `[not stored]`, and the reason is a fixed code in both the log
line (`request_body_withheld`, `response_body_withheld`) and the meta file
(`request_body`, `response_body`, as `not stored: <code>`):

| Code | Meaning |
|---|---|
| `content_encoding_not_decoded` | Request body in a coding other than gzip, deflate or identity |
| `decode_failed` | Request body could not be decoded |
| `too_large` | Over `max_stored_body_bytes` |
| `credential_in_escaped_form` | Request body still held a key in JSON-escaped form after redaction |
| `response_withheld` | The gateway refused the response or cut it (screening, caps, framing) |
| `no_response` | No upstream response arrived |
| `incomplete` | The response did not finish (disconnect, truncation), or the client left before the response head was sent |
| `write_failed` | The file could not be written |

A bodyless response (204, 304, or the answer to HEAD) is stored as an empty
file, with no reason.

Redaction replaces the literal bytes of every loaded key with `[REDACTED]` in
log lines, meta files, stored bodies and tracebacks on stderr. Structured
values are redacted before they are serialised, so JSON escaping in the log
does not hide a key. It does not find a key encoded some other way, such as
base64.

## Docker: the topology that was checked

These are the commands of the intended topology. The internal network has no
host address on its bridge (`gateway_mode_ipv4=isolated`), so nothing on the
Docker host can be reached through it. That option needs Docker Engine 28.0
or later; for older Docker see
[Docker before 28.0](#docker-before-280-remove-the-bridge-address). The
agent's container can resolve the gateway's container name, so give it a
neutral one.

```sh
docker build -f docker/gateway/Dockerfile -t gateway:dev .

docker network create --internal --subnet 172.31.250.0/24 \
  -o com.docker.network.bridge.gateway_mode_ipv4=isolated agent-internal
docker network create gateway-egress

docker run -d --name edge --network gateway-egress \
  -v /path/to/anthropic_key:/run/secrets/anthropic_key:ro \
  -v "$PWD/runs/<run>/gateway:/var/log/gateway" \
  gateway:dev
docker network connect --ip 172.31.250.10 agent-internal edge

# After /run/gateway-tls/local-ca.crt exists:
docker cp edge:/run/gateway-tls/local-ca.crt ./local-ca.crt

# A placeholder key shaped like a real one, new for each run.
KEY="sk-ant-api03-$(python3 -c 'import secrets; print(secrets.token_urlsafe(70)[:93])')AA"

docker run --network agent-internal \
  --add-host api.anthropic.com:172.31.250.10 \
  -v "$PWD/local-ca.crt:/usr/local/share/ca-certificates/local-ca.crt:ro" \
  -e NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/local-ca.crt \
  -e ANTHROPIC_API_KEY="$KEY" \
  <harness image> ...
```

The image runs as uid 10001; Docker lets it bind 443 inside the container. On
a Linux host, uid 10001 must be able to read the key file and write the log
directory.

**Why the bridge has no address.** On an ordinary internal network Docker
gives the bridge device a host-side address (here 172.31.250.1), and Docker
documents that containers on the network can reach host services listening
on that address. With `com.docker.network.bridge.gateway_mode_ipv4=isolated`
no address is assigned to the bridge. Docker's port-publishing page says the
mode can only be used together with `--internal`. Docker's release notes add
it in Engine 28.0.0: "now accept mode `isolated`, when the network is also
`internal`", and "An `internal` bridge network created with gateway mode
`isolated` does not have an address on the Docker host." The 27.x release
notes list only `nat` and `routed` (27.0). Docker 27.4.0 on this machine
rejects it: "unknown gateway mode isolated".

### Docker before 28.0: remove the bridge address

Create the network without the option, then turn IPv6 off on the bridge and
remove its address before any agent container starts:

```sh
docker network create --internal --subnet 172.31.250.0/24 agent-internal
BR="br-$(docker network inspect -f '{{.Id}}' agent-internal | cut -c1-12)"
# Linux host:
sudo sh -c "sysctl -qw net.ipv6.conf.$BR.disable_ipv6=1 && ip addr flush dev $BR"
# Docker Desktop (the bridge lives in its VM):
docker run --rm --privileged --net=host alpine:3.20 \
  sh -c "sysctl -qw net.ipv6.conf.$BR.disable_ipv6=1 && ip addr flush dev $BR"
```

Turning IPv6 off matters: a flush alone removes the IPv4 address, but the
kernel gives the bridge an IPv6 link-local address (`fe80::...`) again when
the first container attaches and the link comes up. `verify` caught exactly
that on 2026-11-21.

Containers on the network still reach each other and the gateway (the bridge
forwards at layer 2), and Docker's embedded DNS still answers. Docker may put
the address back when the daemon restarts, so flush again after a restart,
and verify every run with `--allow-flushed-bridge`. That flag waives only the
check for the `isolated` option. In its place `verify` inspects the bridge
interface on the Docker host (or Docker Desktop's VM) with a short-lived
host-network helper (`alpine:3.20`, `ip -o addr show dev <bridge>`, pulled if
missing and removed again) and asserts it has no address; the bridge address
must also still not answer from inside the agent container.
Start agent containers after the flush: a container that already talked to
the bridge address keeps its neighbour entry for a while and sees timeouts
instead of "no route to host".

`docker cp` copies only the CA certificate; the CA key stays in the
container. `NODE_EXTRA_CA_CERTS` is enough for both Pi (Node 24) and Claude
Code (a Bun binary). Without it, Claude Code stops with "SSL certificate
verification failed (UNABLE_TO_VERIFY_LEAF_SIGNATURE)" and Pi with "unable to
verify the first certificate". Other programs in the agent's container (curl,
git, Python) read the system store. To add the CA there, run
`update-ca-certificates` as root at container start. The harnesses do not need
it.

**DNS in this topology.** The agent's container uses Docker's embedded
resolver. It answers the names of containers on the internal network (the
gateway's name resolves) and fails every other name with `EAI_AGAIN`. It does
not forward to outside resolvers on an internal network. `api.anthropic.com`
comes from `/etc/hosts`, which `--add-host` writes. The gateway's own DNS is
untouched: it resolves `api.anthropic.com` to Anthropic.

**Verify every run.** `python -m gateway verify <gateway> <agent>` runs on the
host with the standard library and checks from inside a running agent
container. It sends nothing the gateway would forward. It asserts:

- every network of the agent container is internal, and on each one the only
  other container is the gateway;
- every such network has `com.docker.network.bridge.gateway_mode_ipv4=isolated`
  (waived with `--allow-flushed-bridge`, see above), and from inside the
  agent container its bridge address does not answer at all: TCP connections
  to it on ports 22, 80 and 443 fail with `EHOSTUNREACH` or `ENETUNREACH`.
  A refused port means a host stack answered, and a timeout could be a
  firewall drop; both fail. A network with IPv6 enabled must also have
  `gateway_mode_ipv6=isolated`, and each address family's bridge address is
  probed. With `--allow-flushed-bridge`, the bridge interface on the Docker
  host must also show no address;
- each of the gateway's active hosts, read from inside the gateway container
  with `python -m gateway hosts`, resolves inside the agent container to the
  gateway (so an omitted `--add-host` fails), completes a verified TLS
  handshake by name, and gets the gateway's own `400` to `GET *`, which it
  refuses before any routing and never forwards;
- an unconfigured name gets an explicit `unrecognized_name` TLS alert.

It also runs a fixed sample of probes, marked `sample` in the output, which
are not exhaustive:

- a few outside names must not resolve;
- a few internet addresses must be unreachable over TCP and UDP;
- as a second line, the bridge address, `host.docker.internal` and
  `gateway.docker.internal` (when they resolve) must not answer on common
  ports or on any port a running container publishes, every port of a
  published range included;
- the gateway must not answer on a few other ports.

`--host NAME` (repeatable) replaces the expected host list. The probes run
concurrently; an unreachable address takes about 3 seconds to fail.

Output on this machine (2026-11-21, Docker Desktop 27.4.0, so the pre-28
fallback with a flushed bridge and `--allow-flushed-bridge`; Claude Code's
container on its own network; Pi's gave the same; the 15 bridge port lines
are condensed):

```text
PASS  network gwdev-net-claude is internal: internal=True
PASS  network gwdev-net-claude: only peer is gwdev-gateway: other peers=none, gateway attached=True
PASS  network gwdev-net-claude: bridge interface on the Docker host has no address: br-31cb92555a7d: addresses=none
WAIVED  network gwdev-net-claude: com.docker.network.bridge.gateway_mode_ipv4=isolated: not set (default); --allow-flushed-bridge given, the host-side and reachability assertions apply
PASS  network gwdev-net-claude: bridge address 172.31.250.1:22 does not answer: EHOSTUNREACH   (same for 80, 443)
PASS  DNS api.anthropic.com resolves to the gateway: resolved 172.31.250.10
PASS  TLS api.anthropic.com:443 by name: http HTTP/1.1 400 Bad Request
PASS  sample: DNS example.com: EAI_AGAIN       (same for github.com, registry.npmjs.org, pypi.org,
                                                api.openai.com, statsig.anthropic.com)
INFO  DNS host.docker.internal: EAI_AGAIN
INFO  DNS gwdev-gateway: resolved 172.31.250.10
PASS  sample: TCP 1.1.1.1:443: ENETUNREACH     (same for 8.8.8.8:443, 160.79.104.10:443, UDP 8.8.8.8:53)
PASS  sample: TCP bridge 172.31.250.1:<15 ports incl. published 8108>: timeout
PASS  sample: TCP gateway 172.31.250.10:8080: ECONNREFUSED   (same for 22, 80, 8443, 3773)
PASS  TLS example.com via 172.31.250.10 rejected by name: ERR_SSL_TLSV1_UNRECOGNIZED_NAME
43 checks, 0 failed, 1 waived (lines marked sample are a fixed sample, not exhaustive)
```

On the same machine, a network created by the old recipe (no isolation, no
flush) fails the two new assertions: the option is `default (nat)` and the
bridge address answers `ECONNREFUSED` on ports 22, 80 and 443.

Give every agent container its own internal network: with two on one
network, the peer check fails, because they could reach each other.

The `isolated` line of the recipe has not been run on this machine, which has
Docker 27.4.0. **Run the check on every host**, Linux ones especially: there
the bridge address belongs to the host itself.

## Why transparent mode

Both harnesses change their requests when the base URL host is not
`api.anthropic.com` (D-007). Recorded on 2026-11-21 with Claude Code 2.1.330
and Pi 1.6.0 on `claude-haiku-5-5`, comparing the gateway's stored requests:

| | Path-prefix mode | Transparent mode |
|---|---|---|
| Claude Code betas | 8 | 12, adding `prompt-caching-scope-2026-01-05`, `thinking-binding-controls-2026-08-01`, `cache-diagnosis-2026-04-07`, `mid-conversation-system-clear-at-2026-08-21` |
| Claude Code headers | 20 names | 25, adding `x-claude-1p-request-class`, `x-claude-code-prompt-id`, `x-claude-code-request-class`, `x-claude-code-sandy-plover`, `x-client-request-id` |
| Claude Code body | no `diagnostics`; 3 system blocks, no global cache scope | `diagnostics` present; 4 system blocks, one with `cache_control.scope: "global"` |
| Claude Code other calls | none | `GET /api/claude_code/settings`, `GET /api/claude_code/policy_limits` |
| Pi betas | 3 | 4, adding `message-threads-2026-08-12` |
| Pi body | no `thread` field | `thread: {"type":"create"}`, then `continue` on the next turn |
| Pi headers | `accept-encoding: gzip, deflate` | `accept-encoding: br, gzip, deflate, zstd`, plus `sec-fetch-mode: cors` |

What the captures show is what each harness sent to the gateway. Each harness
believes it is on the first-party host, so its requests are its default ones.
That they match a direct connection is inferred from that, not measured,
because a direct call would put the real key in the agent's container. On the
way upstream the gateway changes only the credential and `Accept-Encoding`
(always `identity`).

## Limits

- **Provider-mediated egress is not filtered** (D-008). The gateway does not
  inspect request bodies, so a harness can ask the provider to fetch external
  content (for example server-side web search). Filtering would change what
  the harness sends. The usage summary flags responses that report
  server-side tool use.
- Anyone on the internal network can spend tokens through the gateway,
  including the agent itself, within the allowlist. Every call is logged.
- The agent can see signs of interception: the CA file, its common name,
  `NODE_EXTRA_CA_CERTS` and the `/etc/hosts` entry.
- Screening covers the forms listed under [Screening](#screening) and no
  others.
- Non-streaming responses reach the client only once they have fully
  arrived, because they are screened whole.
- HTTP/1.1 only; ALPN offers only `http/1.1`. Request and response trailers
  are dropped. A client that half-closes before reading loses its response.
- Upstream name resolution uses the system resolver and cannot be cancelled;
  the connect deadline starts after it.
- Per-connection buffers (the socket file buffers and a read buffer until it
  is charged) are outside the buffer budget.
- **OpenAI background responses are not reconciled.** A create request with
  `background: true` can return while the response is still queued, with
  usage available only on a later retrieve request; the summary does not
  count retrieve requests, so token totals for background responses are
  incomplete. No OpenAI harness runs through the gateway yet, and the route
  is untested against the live API.
- **An early upstream rejection during an upload is not relayed.** The
  gateway sends the whole request body before reading the response. If the
  upstream answers early and stops reading, the client gets the gateway's
  502 or 504 instead of the upstream's own error. Request bodies are capped
  at `max_request_bytes` (32 MiB).
- Each request opens a new upstream TLS connection. There is no pool.
- Outside Linux, a client disconnect over TLS is noticed only when the next
  write to the client fails.

## Tests

```sh
uv run pytest tests/gateway
```

The tests run the gateway in-process against a local fake upstream with a
made-up key. The transparent-mode tests put the fake upstream behind TLS with
its own private CA and need the `openssl` command. One test, TLS disconnect
detection, runs only on Linux.
