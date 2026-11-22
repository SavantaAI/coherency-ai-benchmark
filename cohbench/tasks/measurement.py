"""Host service and a separate package process connected by a counted protocol."""

CLIENT_RUN = r'''
import importlib.util
import json
import os
import socket
import sys

sock = socket.socket(fileno=int(sys.argv[1]))
wire = sock.makefile("rwb")


def exchange(message):
    wire.write((json.dumps(message) + "\n").encode())
    wire.flush()
    reply = json.loads(wire.readline())
    if "error" in reply:
        raise ValueError(reply["error"])
    return reply["value"]


class Client:
    def list_ids(self):
        return exchange({"op": "list_ids"})

    def get_rates(self):
        return exchange({"op": "get_rates"})

    def get(self, record_id):
        return exchange({"op": "get", "id": record_id})

    def get_many(self, ids):
        return exchange({"op": "get_many", "ids": ids})


try:
    name = sys.argv[2]
    root = os.path.join(os.getcwd(), name)
    spec = importlib.util.spec_from_file_location(name, os.path.join(root, "__init__.py"), submodule_search_locations=[root])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    result = mod.run(Client())
    wire.write((json.dumps({"result": result}) + "\n").encode())
    wire.flush()
except BaseException:
    pass
'''

SERVICE = r'''
"""Own the service data and counter outside the measured package process."""
import json
import os
import secrets
import socket
import subprocess
import sys

CLIENT_RUN = __CLIENT_RUN__


def main():
    with open(os.path.join(os.path.dirname(__file__), "shape.json"), encoding="utf-8") as fh:
        shape = json.load(fh)
    # Only lengths and public record fields are stored. Actual amounts and
    # rates exist only in this process and in counted responses.
    def number(width):
        low = 10 ** (width - 1)
        return low + secrets.randbelow(9 * low)
    records = shape["records"]
    for position, record in enumerate(records):
        width = record.pop("id_width")
        record["id"] = str(position).zfill(4) + secrets.token_hex(width)[0:width - 4]
        record["amount"] = number(record.pop("amount_width"))
    rates = shape["rates"]
    rates["rates"] = {code: number(width) for code, width in rates["rates"].items()}
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
    try:
        while True:
            line = wire.readline(2_000_000)
            if not line:
                break
            request = json.loads(line)
            if "result" in request:
                correct = int(request["result"] == expected)
                break
            calls += 1
            try:
                op = request["op"]
                if op == "list_ids":
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
                largest = max(largest, len(json.dumps(value, sort_keys=True)))
                response = {"value": value}
            except (ValueError, KeyError, TypeError):
                response = {"error": "invalid service request"}
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


def shape(data):
    return {
        "records": [
            {**{k: v for k, v in record.items() if k not in ("amount", "id")},
             "amount_width": len(str(record["amount"])), "id_width": len(record["id"])}
            for record in data["records"]
        ],
        "rates": {**data["rates"], "rates": {k: len(str(v)) for k, v in data["rates"]["rates"].items()}},
    }
