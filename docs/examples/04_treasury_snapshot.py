"""04 — fiscal snapshot: stitch multiple Treasury endpoints into one shape.

Pure code, no inline tour. See README.md for the angle.
"""

import asyncio
from datetime import date

import httpx
from pydantic import BaseModel

from recorded import attach, query, recorder

API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
HEADERS = {"User-Agent": "recorded-example/0.1"}
TODAY = date.today().isoformat()

SECURITIES = ["Treasury Bills", "Treasury Notes", "Treasury Bonds"]
CURRENCIES = ["Euro Zone-Euro", "United Kingdom-Pound", "Japan-Yen"]


class DebtRow(BaseModel):
    record_date: str
    total_debt: float


class RateRow(BaseModel):
    record_date: str
    security: str
    avg_rate: float


class FxRow(BaseModel):
    record_date: str
    currency: str
    rate_per_usd: float


class FiscalSnapshot(BaseModel):
    as_of: str
    total_debt_usd: float
    treasury_rates: dict[str, float]
    fx_per_usd: dict[str, float]


async def _latest(client: httpx.AsyncClient, path: str, **filters: str) -> dict:
    params: dict[str, str | int] = {"sort": "-record_date", "page[size]": 1, **filters}
    r = await client.get(f"{API}{path}", params=params, headers=HEADERS)
    r.raise_for_status()
    return r.json()["data"][0]


@recorder(kind="treasury.debt", data=DebtRow)
async def fetch_debt() -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        row = await _latest(c, "/v2/accounting/od/debt_to_penny")
    attach("record_date", row["record_date"])
    attach("total_debt", float(row["tot_pub_debt_out_amt"]))
    return row


@recorder(kind="treasury.rates", data=RateRow)
async def fetch_rate(security: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        row = await _latest(
            c,
            "/v2/accounting/od/avg_interest_rates",
            filter=f"security_desc:eq:{security}",
        )
    attach("record_date", row["record_date"])
    attach("security", row["security_desc"])
    attach("avg_rate", float(row["avg_interest_rate_amt"]))
    return row


@recorder(kind="treasury.fx", data=FxRow)
async def fetch_fx(country_currency: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        row = await _latest(
            c,
            "/v1/accounting/od/rates_of_exchange",
            filter=f"country_currency_desc:eq:{country_currency}",
        )
    attach("record_date", row["record_date"])
    attach("currency", row["country_currency_desc"])
    attach("rate_per_usd", float(row["exchange_rate"]))
    return row


async def main() -> None:
    await asyncio.gather(
        fetch_debt(key=f"treasury-debt-{TODAY}"),
        *(fetch_rate(s, key=f"treasury-rate-{s}-{TODAY}") for s in SECURITIES),
        *(fetch_fx(c, key=f"treasury-fx-{c}-{TODAY}") for c in CURRENCIES),
    )

    jobs = list(query(kind="treasury.*", since=TODAY, limit=20))
    debt = next(j.data for j in jobs if j.kind == "treasury.debt" and j.data)
    rates = {
        j.data.security: j.data.avg_rate for j in jobs if j.kind == "treasury.rates" and j.data
    }
    fx = {j.data.currency: j.data.rate_per_usd for j in jobs if j.kind == "treasury.fx" and j.data}

    snap = FiscalSnapshot(
        as_of=TODAY,
        total_debt_usd=debt.total_debt,
        treasury_rates=rates,
        fx_per_usd=fx,
    )
    print(snap.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
