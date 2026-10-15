"""The AgentCore Harness worker (worker/harness.py), for the worker role's GLM 5: background jobs only.

The harness runs its model with the settings scripts/deploy_worker.py gave it when it created or updated the harness
(worker/harness.py harness_definition); a change there needs a redeploy.
"""

from typing import Any

from voice.model_catalog import Choice

PROVIDER = "agentcore"


class AgentCore:
    def setup_problem(self) -> str | None:
        """Why the harness cannot run jobs here, or None."""
        from worker import harness

        return None if harness.load_harness_arn() else "no AgentCore harness: run scripts/deploy_worker.py first"

    def ready(self) -> bool:
        return self.setup_problem() is None

    def worker(self, choice: Choice) -> Any:
        from worker import harness

        return harness.HarnessWorker()


ADAPTER = AgentCore()
