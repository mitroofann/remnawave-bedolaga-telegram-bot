# Approved partner links contract

## Endpoint

`GET /cabinet/referral/partner/status` returns the existing partner status response and adds `partner_terms` plus three ready-to-use campaign URLs. The endpoint uses the authenticated cabinet user; it never accepts a partner `user_id` from the client.

Only an approved partner receives `partner_terms` and campaigns. Pending, rejected, revoked, and ordinary users receive `partner_terms: null` and no personal campaign terms. Campaigns are limited to active campaigns whose `partner_user_id` is the current user.

## Effective partner terms

`partner_terms` is an effective policy snapshot. It combines global legacy settings, the approved partner profile commission, and nullable per-partner overrides. `null` overrides inherit the global value; explicit zero values remain zero. Money fields are integer kopeks, percentages are integers from 0 through 100, and payment caps are integers.

Legacy recurring commission tiers are returned as structured objects:

```json
{"payment_number": 10, "percent": 15}
```

The legacy `10:15` representation remains unchanged in admin settings APIs. In the levels scheme, `levels_mode` is `chain` or `tiers`, and `levels` contains the normalized level rules used by the reward engine.

## Campaign URLs

Every campaign keeps the existing `deep_link`, `web_link`, and `start_parameter`. The existing `start_parameter` is also the sole `campaign` query value for the new URLs; no canonical-slug column or migration is required.

For `start_parameter = ` `promo/summer` (encoded as `promo%2Fsummer`), the URLs are:

- Sale: `https://cabinet.bulkavpn.net/buy/now?campaign=promo%2Fsummer`
- Trial: `https://cabinet.bulkavpn.net/buy/now?campaign=promo%2Fsummer&intent=trial`
- Public landing: `https://bulkavpn.net/?campaign=promo%2Fsummer`

The campaign value is encoded exactly once. Trial navigation preserves both `campaign` and `intent=trial`.

## Attribution

Regular landing purchases continue to persist the validated campaign value in `GuestPurchase.campaign_slug`. Paid landing trials use the same purchase record and attribute after successful trial fulfillment. Free landing trials attribute after the user and trial subscription have been committed. Attribution is best-effort and never prevents successful trial delivery. Existing campaign ownership, idempotency, self-attribution prevention, and gift exclusion remain unchanged.

Omitting the new trial campaign field preserves the previous behavior.
