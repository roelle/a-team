"""A runner a test team names in its roster's supervisor block."""
from agyteam.runner import Runner


class TeamRunner(Runner):
    label = "team-runner"

    def wake(self, agent, message):
        return "ok"


class ListingRunner(TeamRunner):
    """Offers whatever its roster runner_config lists, without a network."""
    label = "listing-runner"

    @classmethod
    def list_models(cls, config=None):
        return [{"id": m, "label": m} for m in (config or {}).get("offers", [])]
