"""Tomorrow's costs from four matching weekdays of half-hourly meter readings."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from statistics import mean
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from comparison_engine import CostBreakdown, TariffComparison
from tariff import TARIFFS

LONDON = ZoneInfo("Europe/London")
HALF_HOUR = timedelta(minutes=30)


class PredictionError(ValueError):
    """An actionable prediction failure whose text is safe to display."""


class RatesNotPublished(PredictionError):
    """Agile does not yet cover the expected day-ahead publication period."""


def prediction_tariffs(value):
    ids = list(dict.fromkeys(part.strip().lower() for part in value.split(",") if part.strip()))
    if "agile" not in ids:
        raise PredictionError("Prediction tariffs must include agile.")
    known = {tariff.id: tariff for tariff in TARIFFS}
    if any(identifier not in known for identifier in ids):
        raise PredictionError("Unknown prediction tariff. Valid IDs: " + ", ".join(known))
    return [deepcopy(known[identifier]) for identifier in ids]


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise PredictionError("Octopus returned a timestamp without a timezone.")
    return result.astimezone(UTC)


def day_bounds(day):
    return (datetime.combine(day, time.min, LONDON).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time.min, LONDON).astimezone(UTC))


def day_slots(day):
    start, end = day_bounds(day)
    slots = []
    while start < end:
        slots.append(start)
        start += HALF_HOUR
    return slots


def clock_slot(moment):
    local = moment.astimezone(LONDON)
    return local.hour, local.minute


def finite_number(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PredictionError(f"Missing or invalid {label}.") from None
    if not isfinite(number):
        raise PredictionError(f"Missing or invalid {label}.")
    return number


@dataclass
class PredictionResult:
    target_date: date
    source_dates: list[date]
    profile: list[dict]
    comparisons: list[TariffComparison]

    def as_dict(self):
        ranked = sorted(self.comparisons, key=lambda entry: entry.total_cost)
        best = ranked[0]
        saving = ranked[1].total_cost - best.total_cost if len(ranked) > 1 else 0
        day_slot_count = len(day_slots(self.target_date))
        return {
            "target_date": self.target_date.isoformat(),
            "source_dates": [day.isoformat() for day in self.source_dates],
            "total_kwh": sum(slot["kwh"] for slot in self.profile),
            "slot_count": len(self.profile),
            "day_slot_count": day_slot_count,
            "is_full_day": len(self.profile) == day_slot_count,
            "period_from": self.profile[0]["start"].astimezone(LONDON).isoformat(),
            "period_to": (self.profile[-1]["start"] + HALF_HOUR).astimezone(LONDON).isoformat(),
            "best_tariff": best.tariff.display_name,
            "saving_pounds": saving / 100,
            "comparisons": [
                {"tariff": entry.tariff.display_name, "tariff_id": entry.tariff.id,
                 "total_pounds": entry.cost_breakdown.total_cost_pounds,
                 "usage_pounds": entry.cost_breakdown.consumption_cost_pounds,
                 "standing_pounds": entry.cost_breakdown.standing_charge_pounds}
                for entry in ranked
            ],
            "slots": [{"start": slot["start"].astimezone(LONDON).isoformat(),
                       "kwh": slot["kwh"], "rates_pence": slot["rates_pence"],
                       "costs_pence": {tariff_id: slot["kwh"] * price
                                       for tariff_id, price in slot["rates_pence"].items()}}
                      for slot in self.profile],
        }

    def summary(self):
        result = self.as_dict()
        lines = [f"PREDICTION — {self.target_date:%A %d %B %Y} (Europe/London)",
                 "Source days: " + ", ".join(day.isoformat() for day in self.source_dates),
                 f"Estimated consumption: {result['total_kwh']:.3f} kWh across {result['slot_count']} half hours."]
        start = self.profile[0]["start"].astimezone(LONDON)
        end = (self.profile[-1]["start"] + HALF_HOUR).astimezone(LONDON)
        lines.append(f"Comparison period: {start:%Y-%m-%d %H:%M %Z} to {end:%Y-%m-%d %H:%M %Z}.")
        if not result["is_full_day"]:
            lines.append(f"Partial-day estimate: {result['slot_count']} of {result['day_slot_count']} half hours. "
                         "The final hour has no published Agile rates and is excluded from every tariff.")
        lines.append("Includes one full daily standing charge for each tariff.")
        for entry in result["comparisons"]:
            lines.append(f"{entry['tariff']}: £{entry['total_pounds']:.2f} "
                         f"(£{entry['usage_pounds']:.2f} usage + £{entry['standing_pounds']:.2f} standing charge)")
        lines.append(f"Estimated cheapest: {result['best_tariff']}.")
        if len(self.comparisons) > 1:
            lines.append(f"Saving against the next cheapest: £{result['saving_pounds']:.2f}.")
        lines.append("Estimate based on the same half hour on the previous four matching weekdays. No tariff change requested.")
        return "\n".join(lines)


class PredictionEngine:
    def __init__(self, query_service):
        self.service = query_service
        self.base_url = query_service.base_url.rstrip("/")

    def _pages(self, url, *, authenticated=False):
        records, visited = [], set()
        while url:
            if url in visited:
                raise PredictionError("Octopus returned a repeated pagination link.")
            visited.add(url)
            data = self.service.execute_rest_query(url, authenticated=authenticated)
            if not isinstance(data.get("results"), list):
                raise PredictionError("Octopus returned an incomplete data response.")
            records.extend(data["results"])
            url = urljoin(url, data["next"]) if data.get("next") else None
        return records

    @staticmethod
    def _period_url(url, day):
        start, end = day_bounds(day)
        return url + ("&" if "?" in url else "?") + urlencode({
            "period_from": start.isoformat(), "period_to": end.isoformat(), "page_size": 1500,
        })

    def _meter(self, account_number):
        account = self.service.execute_rest_query(
            f"{self.base_url}/accounts/{account_number}/", authenticated=True)
        now = datetime.now(UTC)
        candidates = []
        for property_ in account.get("properties", []):
            if property_.get("moved_out_at") and timestamp(property_["moved_out_at"]) <= now:
                continue
            for point in property_.get("electricity_meter_points", []):
                if point.get("is_export") is not False:
                    continue
                agreements = [entry for entry in point.get("agreements", [])
                              if timestamp(entry["valid_from"]) <= now
                              and (not entry.get("valid_to") or now < timestamp(entry["valid_to"]))]
                if agreements:
                    candidates.append((point, max(agreements, key=lambda entry: timestamp(entry["valid_from"]))))
        if len(candidates) != 1:
            raise PredictionError("Prediction requires one identifiable active import electricity meter point.")
        point, agreement = candidates[0]
        serials = list(dict.fromkeys(meter["serial_number"] for meter in point.get("meters", [])
                                    if meter.get("serial_number")))
        if not serials or not point.get("mpan"):
            raise PredictionError("No electricity meter serial number or MPAN is available for prediction.")
        return point["mpan"], serials, agreement["tariff_code"][-1]

    def _profile(self, mpan, serials, target_date):
        source_dates = [target_date - timedelta(weeks=weeks) for weeks in range(1, 5)]
        histories = []
        for day in source_dates:
            readings = {}
            expected = day_slots(day)
            for serial in serials:
                url = f"{self.base_url}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
                for reading in self._pages(self._period_url(url, day), authenticated=True):
                    start = timestamp(reading["interval_start"])
                    if not expected[0] <= start < expected[-1] + HALF_HOUR:
                        continue
                    if timestamp(reading["interval_end"]) - start != HALF_HOUR:
                        raise PredictionError(f"Consumption for {day} is not half-hourly.")
                    kwh = finite_number(reading.get("consumption"), "consumption")
                    if kwh < 0:
                        raise PredictionError(f"Negative import consumption returned for {day}.")
                    if start in readings and readings[start] != kwh:
                        raise PredictionError(f"Overlapping meter readings disagree for {day}.")
                    readings[start] = kwh
            if set(readings) != set(expected):
                raise PredictionError(f"Incomplete half-hourly consumption for {day}: "
                                      f"{len(readings)} of {len(expected)} slots. Four complete matching weekdays are required.")
            by_clock = {}
            for start, kwh in readings.items():
                by_clock.setdefault(clock_slot(start), []).append(kwh)
            # Give each source day equal weight in the repeated autumn hour.
            histories.append({clock: mean(values) for clock, values in by_clock.items()})
        profile = []
        for start in day_slots(target_date):
            values = [history[clock_slot(start)] for history in histories if clock_slot(start) in history]
            if not values:
                raise PredictionError("No matching historical half hour is available.")
            # A complete spring clock-change day has no 01:00/01:30 local slots.
            profile.append({"start": start, "kwh": mean(values)})
        return source_dates, profile

    @staticmethod
    def _rate_at(rates, start, end):
        matches = [entry for entry in rates
                   if entry.get("payment_method") in (None, "DIRECT_DEBIT")
                   and timestamp(entry["valid_from"]) <= start
                   and (not entry.get("valid_to") or timestamp(entry["valid_to"]) >= end)]
        # Some products expose a separate direct-debit rate alongside a default.
        direct_debit = [entry for entry in matches if entry.get("payment_method") == "DIRECT_DEBIT"]
        values = {finite_number(entry.get("value_inc_vat"), "VAT-inclusive rate")
                  for entry in (direct_debit or matches)}
        if len(values) > 1:
            raise PredictionError("Octopus returned conflicting rates for a half hour.")
        return next(iter(values)) if values else None

    def _rates(self, tariff, region, day, products, *, slots=None):
        matching = [product for product in products
                    if product.get("display_name") == tariff.api_display_name
                    and product.get("direction") == "IMPORT"]
        if not matching:
            raise PredictionError(f"No available product found for {tariff.display_name}.")
        product = max(matching, key=lambda entry: entry.get("available_from") or "")
        details = self.service.execute_rest_query(f"{self.base_url}/products/{product['code']}/")
        region_details = details.get("single_register_electricity_tariffs", {}).get("_" + region, {})
        region_tariff = region_details.get("direct_debit_monthly") or region_details.get("varying")
        if not region_tariff:
            raise PredictionError(f"No regional rates found for {tariff.display_name}.")
        links = {entry["rel"].lower(): entry["href"] for entry in region_tariff.get("links", [])}
        if "standard_unit_rates" not in links or "standing_charges" not in links:
            raise PredictionError(f"Rate links are unavailable for {tariff.display_name}.")
        units = self._pages(self._period_url(links["standard_unit_rates"], day))
        slots = day_slots(day) if slots is None else slots
        unit_prices = [self._rate_at(units, start, start + HALF_HOUR) for start in slots]
        if any(price is None for price in unit_prices):
            if tariff.id == "agile":
                # Day-ahead Agile publication ends at 23:00 UK time. Accept
                # only that exact trailing hour being absent, never gaps or
                # an arbitrary subset of rates. Keep a full day when available.
                cutoff = datetime.combine(day, time(23), LONDON).astimezone(UTC)
                expected_count = sum(start < cutoff for start in slots)
                if (all(price is not None for price in unit_prices[:expected_count])
                        and all(price is None for price in unit_prices[expected_count:])):
                    unit_prices = unit_prices[:expected_count]
                else:
                    published = sum(price is not None for price in unit_prices[:expected_count])
                    if published == expected_count:
                        raise RatesNotPublished(f"Agile rates for {day} have incomplete late-evening coverage. "
                                                "Expected rates through 23:00 Europe/London or the full day.")
                    raise RatesNotPublished(f"Agile rates for {day} are not yet fully available "
                                            f"({published} of {expected_count} half hours before 23:00 Europe/London).")
            else:
                raise PredictionError(f"Rates for {tariff.display_name} do not cover the full prediction period.")
        standing = self._pages(self._period_url(links["standing_charges"], day))
        standing_prices = [self._rate_at(standing, start, start + HALF_HOUR) for start in day_slots(day)]
        if any(price is None for price in standing_prices):
            raise PredictionError(f"Standing charges are incomplete for {tariff.display_name}.")
        return unit_prices, mean(standing_prices)

    def predict(self, account_number, target_date, tariff_ids):
        tariffs = prediction_tariffs(tariff_ids)
        mpan, serials, region = self._meter(account_number)
        products = self._pages(f"{self.base_url}/products/?brand=OCTOPUS_ENERGY&is_business=false")
        # Check Agile publication before downloading historical usage or pricing others.
        tariffs.sort(key=lambda tariff: tariff.id != "agile")
        rates = {tariffs[0].id: self._rates(tariffs[0], region, target_date, products)}
        source_dates, profile = self._profile(mpan, serials, target_date)
        profile = profile[:len(rates[tariffs[0].id][0])]
        for tariff in tariffs[1:]:
            rates[tariff.id] = self._rates(tariff, region, target_date, products,
                                          slots=[slot["start"] for slot in profile])
        comparisons = []
        total_kwh = sum(slot["kwh"] for slot in profile)
        for index, slot in enumerate(profile):
            slot["rates_pence"] = {tariff.id: rates[tariff.id][0][index] for tariff in tariffs}
        for tariff in tariffs:
            prices, standing = rates[tariff.id]
            usage_cost = sum(slot["kwh"] * price for slot, price in zip(profile, prices, strict=True))
            comparisons.append(TariffComparison(tariff, CostBreakdown(
                usage_cost, standing, usage_cost + standing, total_kwh)))
        return PredictionResult(target_date, source_dates, profile, comparisons)
