"""Shared utility for generating campaign deep links and web links."""

from urllib.parse import quote

from app.config import settings


_PARTNER_SALE_BASE_URL = 'https://cabinet.bulkavpn.net/buy/now'
_PARTNER_LANDING_BASE_URL = 'https://bulkavpn.net/'


def _encode_campaign_parameter(start_parameter: str) -> str:
    """Encode a raw campaign identifier once for a query string."""
    return quote(start_parameter, safe='')


def get_partner_campaign_sale_url(start_parameter: str | None) -> str | None:
    """Build the fixed-domain sale URL for an approved partner campaign."""
    if not start_parameter:
        return None
    return f'{_PARTNER_SALE_BASE_URL}?campaign={_encode_campaign_parameter(start_parameter)}'


def get_partner_campaign_trial_url(start_parameter: str | None) -> str | None:
    """Build the fixed-domain trial URL for an approved partner campaign."""
    if not start_parameter:
        return None
    encoded = _encode_campaign_parameter(start_parameter)
    return f'{_PARTNER_SALE_BASE_URL}?campaign={encoded}&intent=trial'


def get_partner_campaign_landing_url(start_parameter: str | None) -> str | None:
    """Build the fixed-domain public landing URL for an approved partner campaign."""
    if not start_parameter:
        return None
    return f'{_PARTNER_LANDING_BASE_URL}?campaign={_encode_campaign_parameter(start_parameter)}'


def get_campaign_deep_link(start_parameter: str) -> str:
    """Generate a Telegram deep link for a campaign."""
    bot_username = settings.get_bot_username()
    if bot_username:
        return f'https://t.me/{bot_username}?start={start_parameter}'
    return f'?start={start_parameter}'


def get_campaign_web_link(start_parameter: str) -> str | None:
    """Generate a web app link for a campaign.

    Prefers CABINET_URL (where the auth flow captures ?campaign= param),
    falls back to MINIAPP_CUSTOM_URL for backwards compatibility.
    """
    cabinet_url = settings._normalized_cabinet_url()
    if cabinet_url:
        sep = '&' if '?' in cabinet_url else '?'
        return f'{cabinet_url}{sep}campaign={start_parameter}'

    base_url = (settings.MINIAPP_CUSTOM_URL or '').rstrip('/')
    if base_url:
        return f'{base_url}/?campaign={start_parameter}'
    return None
