"""The task metric: how many job logs exist under out/."""
import json, os
print(json.dumps({"logs": sum(1 for n in os.listdir("out") if n.endswith(".log")) if os.path.isdir("out") else 0}))
