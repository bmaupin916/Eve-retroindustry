"""Single source of truth for the application version and the User-Agent built
from it.

Lives here rather than in `app/web/main.py` because both `app/esi/client.py` and
`app/sde/feed.py` need the User-Agent, and neither may import the web layer.
Bump APP_VERSION on every release; `main.py` re-exports it.
"""

APP_VERSION = "0.9.28"

# Contact path CCP's best-practices guide asks every consumer of their services
# to publish, so they can reach a human before they reach for a ban.
APP_CONTACT = "brian.maupin@gmail.com"
APP_URL = "https://github.com/EVERetroIndustry/Eve-retroindustry"


def user_agent(component: str | None = None) -> str:
    """The User-Agent sent to every CCP service (ESI, SSO, the SDE feed).

    On the desktop build each user called out from their own IP, so identifying
    ourselves was cosmetic. Hosted, every request arrives from one IP under one
    applicationID — an unidentified high-volume source with no contact path is
    exactly the profile CCP's guide says "can lead to your app being banned".

    `component` tags which subsystem is calling (e.g. "import_sde"), so a spike
    in CCP's logs can be traced to one part of the app rather than the whole of
    it. The version and contact details are identical either way.
    """
    name = f"EVE-Retroindustry/{APP_VERSION}"
    if component:
        name += f" ({component})"
    return f"{name} ({APP_CONTACT}; +{APP_URL})"


USER_AGENT = user_agent()
