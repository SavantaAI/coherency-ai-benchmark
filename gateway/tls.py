"""Private CA and per-host leaf certificates for transparent mode.

A new CA is generated at every gateway start with the openssl command. Its
private key stays in `tls_dir` inside the gateway container. Only the CA
certificate is meant to leave it. Each active host gets its own leaf
certificate, chosen by the TLS server name, so a client sees only the name it
asked for.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

CA_CNF = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = {name}
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
"""

LEAF_EXT = """\
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = DNS:{host}
"""


def _openssl(*args: str, cwd: Path) -> None:
    exe = shutil.which("openssl")
    if exe is None:
        raise RuntimeError("transparent mode needs the openssl command")
    result = subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {result.stderr.strip()}")


def _install(src: Path, dst: Path, mode: int) -> Path:
    os.chmod(src, mode)  # before the file appears under its final name
    os.replace(src, dst)
    return dst


def generate(
    tls_dir: str | os.PathLike,
    hosts: list[str],
    days: int = 7,
    ca_name: str = "Local Root CA",
    ca_cert_file: str = "ca.crt",
) -> dict:
    """Create the CA and one leaf per host.

    Returns {"ca.key": path, "ca.crt": path, "leaves": {host: (crt, key)}}.
    """
    if not hosts:
        raise ValueError("no hostnames to issue certificates for")
    out = Path(tls_dir)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    result: dict = {"leaves": {}}
    with tempfile.TemporaryDirectory(dir=out) as work_dir:
        work = Path(work_dir)
        os.chmod(work, 0o700)
        (work / "ca.cnf").write_text(CA_CNF.format(name=ca_name))
        _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key", cwd=work)
        _openssl(
            "req", "-x509", "-new", "-key", "ca.key", "-config", "ca.cnf", "-extensions", "v3_ca",
            "-days", str(days), "-sha256", "-out", "ca.crt", cwd=work,
        )
        for index, host in enumerate(hosts):
            stem = f"leaf-{index}"
            (work / f"{stem}.ext").write_text(LEAF_EXT.format(host=host))
            _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{stem}.key", cwd=work)
            _openssl("req", "-new", "-key", f"{stem}.key", "-subj", f"/CN={host}", "-out", f"{stem}.csr", cwd=work)
            _openssl(
                "x509", "-req", "-in", f"{stem}.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                "-set_serial", "0x" + os.urandom(16).hex(), "-days", str(days), "-sha256",
                "-extfile", f"{stem}.ext", "-out", f"{stem}.crt", cwd=work,
            )
            key = _install(work / f"{stem}.key", out / f"{stem}.key", 0o600)
            crt = _install(work / f"{stem}.crt", out / f"{stem}.crt", 0o644)
            result["leaves"][host] = (crt, key)
        result["ca.key"] = _install(work / "ca.key", out / "ca.key", 0o600)
        result["ca.crt"] = _install(work / "ca.crt", out / ca_cert_file, 0o644)
    return result


def _context(crt: Path | None = None, key: Path | None = None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # The gateway speaks HTTP/1.1 only. Clients that offer h2 fall back.
    ctx.set_alpn_protocols(["http/1.1"])
    if crt is not None:
        ctx.load_cert_chain(crt, key)
    return ctx


def server_context(leaves: dict, on_refused=None) -> ssl.SSLContext:
    """TLS server context that completes a handshake only for a configured name.

    `leaves` maps hostname to (certificate, key). The accepted name is stored
    on the socket as `gateway_sni`; a refused one too, for the log.
    """
    per_host = {h.lower(): _context(crt, key) for h, (crt, key) in leaves.items()}
    base = _context()

    def sni_callback(sock, server_name, _ctx):
        name = (server_name or "").lower()
        sock.gateway_sni = name or None
        ctx = per_host.get(name)
        if ctx is None:
            if on_refused is not None:
                on_refused(name or None)
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        sock.context = ctx
        return None

    base.sni_callback = sni_callback
    return base
