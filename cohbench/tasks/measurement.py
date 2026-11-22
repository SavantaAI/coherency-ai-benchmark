"""Host service and a separate package process connected by a counted protocol."""

from .client_protocol import NORMALIZE

CLIENT_RUN = r'''
import importlib
import json
import os
import socket
import sys

sys.pycache_prefix = os.devnull
sock = socket.socket(fileno=int(sys.argv[1]))
wire = sock.makefile("rwb")


def exchange(message):
    wire.write((json.dumps(message) + "\n").encode())
    wire.flush()
    reply = json.loads(wire.readline())
    if "error" in reply:
        kind = {"ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError}[reply["error"]]
        raise kind(*reply["args"])
    return reply["value"]


class Client:
    def list_ids(self):
        return exchange({"op": "list_ids"})

    def get_snapshot(self):
        return exchange({"op": "get_snapshot"})

    def get_rates(self):
        return exchange({"op": "get_rates"})

    def get(self, record_id):
        return exchange({"op": "get", "id": normalize_id(record_id)})

    def get_many(self, ids):
        return exchange({"op": "get_many", "ids": normalize_ids(ids)})


try:
    name = sys.argv[2]
    sys.path.insert(0, os.getcwd())
    mod = importlib.import_module(name)
    result = mod.run(Client())
    wire.write((json.dumps({"result": result}) + "\n").encode())
    wire.flush()
except BaseException:
    pass
'''

CLIENT_RUN = CLIENT_RUN.replace("class Client:", NORMALIZE + "\n\nclass Client:")

SERVICE = r'''
"""Own the service data and counter outside the measured package process."""
import json
import os
import hashlib
import socket
import subprocess
import sys

CLIENT_RUN = __CLIENT_RUN__


def workload():
    if len(sys.argv) == 3 and sys.argv[1] != "seed-fd":
        # The watcher supplies argv only. Re-execute without the seed in argv
        # before starting a package that can inspect its parent's command.
        reader, writer = os.pipe()
        payload = json.dumps({"key": sys.argv[1], "shape": json.loads(sys.argv[2])}).encode()
        writer_pid = os.fork()
        if writer_pid == 0:
            os.close(reader)
            with os.fdopen(writer, "wb") as stream:
                stream.write(payload)
            os._exit(0)
        os.close(writer)
        os.set_inheritable(reader, True)
        os.execv(sys.executable, [sys.executable, "-I", "-B", __file__, "seed-fd", str(reader), str(writer_pid)])
    if len(sys.argv) != 4 or sys.argv[1] != "seed-fd":
        raise ValueError("missing workload key")
    with os.fdopen(int(sys.argv[2]), "rb") as stream:
        payload = json.load(stream)
    os.waitpid(int(sys.argv[3]), 0)
    key = bytes.fromhex(payload["key"])
    if len(key) != 16:
        raise ValueError("invalid workload key")
    return key, payload["shape"]


def main():
    key, shape = workload()
    counter = 0

    def below(limit):
        nonlocal counter
        ceiling = (1 << 256) - ((1 << 256) % limit)
        while True:
            value = int.from_bytes(hashlib.sha256(key + counter.to_bytes(8, "big")).digest(), "big")
            counter += 1
            if value < ceiling:
                return value % limit

    records = shape["records"]
    for position, record in enumerate(records):
        record["note"] = " ".join(shape["note_words"][i] for i in record.pop("note_tokens"))
        prefix = record.pop("id_prefix")
        record["id"] = prefix + "-" + str(10000 + position * 7 + below(7))
        low, high = record.pop("amount_range")
        record["amount"] = low + below(high - low + 1)
    rates = shape["rates"]
    rates["rates"] = {code: low + below(high - low + 1) for code, (low, high) in rates["rates"].items()}
    index = {record["id"]: record for record in records}
    expected = {}
    for record in records:
        category = record["category"]
        expected[category] = expected.get(category, 0) + record["amount"] * rates["rates"][record["currency"]] // 10000
    parent, child = socket.socketpair()
    parent.settimeout(20)
    proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", CLIENT_RUN, str(child.fileno()), "__PKG__"],
        pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.close()
    wire = parent.makefile("rwb")
    calls, largest, correct = 0, 0, 0
    allowed = True
    try:
        while True:
            line = wire.readline(2_000_000)
            if not line:
                break
            request = json.loads(line)
            if "result" in request:
                correct = int(request["result"] == expected and allowed)
                break
            try:
                op = request["op"]
                if op == "get_snapshot":
                    allowed = False
                    value = {"records": records, "rates": rates}
                elif op == "list_ids":
                    value = list(index)
                elif op == "get_rates":
                    value = rates
                elif op == "get":
                    value = index[request["id"]]
                elif op == "get_many":
                    ids = request["ids"]
                    if not isinstance(ids, list) or len(ids) > 50:
                        raise ValueError("get_many takes at most 50 ids")
                    value = [index[i] for i in ids]
                else:
                    raise ValueError("unknown operation")
                calls += 1
                largest = max(largest, len(json.dumps(value, sort_keys=True)))
                response = {"value": value}
            except (ValueError, KeyError, TypeError) as error:
                response = {"error": type(error).__name__, "args": error.args}
            wire.write((json.dumps(response) + "\n").encode())
            wire.flush()
    except (OSError, ValueError, TypeError):
        correct = 0
    finally:
        wire.close()
        parent.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    reading = {"peak_kb": round(largest / 1024.0, 1), "correct": correct,
        "checked_round_trips": calls if correct else 999999}
    if correct:
        reading["round_trips"] = calls
    print(json.dumps(reading, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def source():
    return SERVICE.replace("__CLIENT_RUN__", repr(CLIENT_RUN))


def shape(data, currencies):
    words = sorted({word for record in data["records"] for word in record["note"].split()})
    indexes = {word: i for i, word in enumerate(words)}
    return {
        "note_words": words,
        "records": [
            {**{k: v for k, v in record.items() if k not in ("amount", "id", "note")},
             "note_tokens": [indexes[word] for word in record["note"].split()],
             "amount_range": [max(500, 10 ** (len(str(record["amount"])) - 1)), min(50000, 10 ** len(str(record["amount"])) - 1)],
             "id_prefix": record["id"].rsplit("-", 1)[0]}
            for record in data["records"]
        ],
        "rates": {**data["rates"], "rates": {code: [low, high] for code, low, high in currencies}},
    }
