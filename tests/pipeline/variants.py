"""Transform only authored profile steps, leaving task expectations intact."""

import copy
import re


def profile_runs(command):
    return len(re.findall(r"(?<![\w/])\./bin/profile(?=\s|$|[;&|])", command))


def transform(document, variant):
    doc = copy.deepcopy(document)
    changes = 0
    for handler in doc["script"]["handlers"]:
        steps = handler["steps"]
        if variant == "batched":
            for i in range(len(steps) - 2):
                step = steps[i]
                if step.get("do") == "bash" and profile_runs(step["command"]) == 1 and steps[i:i + 3] == [step] * 3:
                    steps[i:i + 3] = [{**step, "command": " && ".join([step["command"]] * 3)}]
                    changes += 1
                    break
        elif variant == "cut":
            for step in steps:
                if step.get("do") == "bash" and profile_runs(step["command"]):
                    # Keep the human-readable captures required by the script.
                    step["command"] += " | sed '/^sync-profile-v1: /d'"
                    changes += 1
        else:
            raise ValueError(variant)
    assert changes > 0
    return doc


def script_runs(document, state):
    total = 0
    for status in state["handlers"]["main"]:
        steps = document["script"]["handlers"][status["index"]]["steps"]
        assert all(step["do"] in ("bash", "read", "write", "edit") for step in steps)
        total += sum(profile_runs(step["command"]) for step in steps[:status["steps_emitted"]]
                     if step["do"] == "bash")
    return total
