"""IMF Real Exchange Rate Explorer — Python 3.12 / Streamlit Community Cloud."""

from __future__ import annotations

from datetime import date
from io import StringIO
import re
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

BASE_URL = "https://api.imf.org/external/sdmx/3.0"
EARLIEST_MONTH = pd.Period("1976-01", freq="M")
CSV_COLUMNS = [
    "month", "cpi_country_1", "cpi_country_2", "fx_country_1_per_usd",
    "fx_country_2_per_usd", "fx_country_1_source", "fx_country_2_source",
    "nominal_s_country_2_per_country_1", "rer", "rer_sample_mean",
    "real_index", "valid_rer_observation",
]


class IMFDataError(RuntimeError):
    """Raised for a failed, unavailable, or unexpectedly formatted IMF response."""


def http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2, backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def request_imf(url: str, *, params: dict | None = None, accept: str = "text/csv") -> requests.Response:
    try:
        with http_session() as session:
            response = session.get(
                url, params=params, headers={"Accept": accept}, timeout=(10, 35)
            )
    except requests.exceptions.RequestException as exc:
        raise IMFDataError(f"Could not reach the IMF API: {exc}") from exc
    if response.status_code == 204:
        return response
    if response.status_code in (401, 403, 429):
        raise IMFDataError(
            f"IMF API returned HTTP {response.status_code}. "
            "Access may be blocked, temporarily limited, or under maintenance."
        )
    if not response.ok:
        raise IMFDataError(
            f"IMF API returned HTTP {response.status_code} for {response.url}. "
            "Please try again later or choose another country."
        )
    return response


def parse_codelist(content: bytes) -> dict[str, str]:
    """Read country names from either SDMX Structure-JSON or Structure-XML."""
    result: dict[str, str] = {}
    try:
        import json
        payload = json.loads(content)
        data = payload.get("data", payload)
        groups = data.get("codelists", [])
        if isinstance(groups, dict):
            groups = list(groups.values())
        for group in groups:
            if not isinstance(group, dict):
                continue
            codes = group.get("codes", [])
            if isinstance(codes, dict):
                codes = list(codes.values())
            for item in codes:
                if not isinstance(item, dict):
                    continue
                code = item.get("id")
                name = item.get("name")
                if isinstance(name, dict):
                    name = name.get("en") or next(iter(name.values()), None)
                if isinstance(code, str) and isinstance(name, str):
                    result[code] = name
        if result:
            return result
    except (ValueError, TypeError, AttributeError):
        pass

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise IMFDataError("The IMF country codelist was not valid JSON or XML.") from exc
    for node in root.iter():
        if node.tag.split("}")[-1] != "Code":
            continue
        code = node.get("id")
        names = [n for n in node if n.tag.split("}")[-1] == "Name"]
        name = next(
            (n.text for n in names if n.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"),
            None,
        ) or next((n.text for n in names if n.text), None)
        if code and name:
            result[code] = name
    return result


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def country_options() -> tuple[dict[str, str], str | None]:
    """Use IMF's shared CL_COUNTRY; retain an ISO fallback if metadata is down."""
    countries: dict[str, str] = {}
    issue: str | None = None
    for version in ("+", "~"):
        try:
            response = request_imf(
                f"{BASE_URL}/structure/codelist/IMF/CL_COUNTRY/{version}",
                accept="application/vnd.sdmx.structure+json;version=2.0, application/xml",
            )
            if response.status_code == 204:
                continue
            names = parse_codelist(response.content)
            countries = {
                code: name
                for code, name in names.items()
                if re.fullmatch(r"[A-Z]{3}", code) is not None
            }
            if len(countries) >= 100:
                break
        except IMFDataError as exc:
            issue = str(exc)
    if len(countries) < 100:
        countries = {c.alpha_3: c.name for c in pycountry.countries}
        issue = (
            "IMF country metadata could not be loaded. The menu temporarily uses ISO-3 "
            "country/territory names; some may have no IMF data."
        )
    countries["G163"] = "Euro area"
    countries["GBR"] = "United Kingdom"
    countries["USA"] = "United States"
    return countries, issue


def parse_months(values: pd.Series) -> pd.Series:
    """Normalize IMF YYYY-M01 and common YYYY-01 variants without date guessing."""
    raw = values.astype("string").str.strip()
    pieces = raw.str.extract(r"^(\d{4})-?M?(\d{2})$")
    normalized = pieces[0].fillna("") + "-" + pieces[1].fillna("") + "-01"
    timestamps = pd.to_datetime(normalized, format="%Y-%m-%d", errors="coerce")
    return timestamps.dt.to_period("M")


def parse_observations(content: bytes, expected_codes: tuple[str, ...]) -> pd.DataFrame:
    """Convert IMF SDMX-CSV to one value per (COUNTRY, month)."""
    try:
        frame = pd.read_csv(StringIO(content.decode("utf-8-sig")), dtype=str)
    except (UnicodeError, pd.errors.ParserError, ValueError) as exc:
        raise IMFDataError("Could not decode the IMF response as SDMX-CSV.") from exc

    frame.columns = [str(c).strip().split(":", 1)[0].upper() for c in frame.columns]
    if "TIME_PERIOD" not in frame or "OBS_VALUE" not in frame:
        raise IMFDataError(
            "The IMF API response did not contain TIME_PERIOD and OBS_VALUE. "
            "It may have returned an error or an unsupported format."
        )
    if "COUNTRY" not in frame:
        if len(expected_codes) == 1:
            frame["COUNTRY"] = expected_codes[0]
        else:
            raise IMFDataError("Multi-country IMF response has no COUNTRY column.")

    frame = frame[frame["COUNTRY"].isin(expected_codes)].copy()
    if frame.empty:
        return pd.DataFrame(columns=["COUNTRY", "month", "value"])

    frame["month"] = parse_months(frame["TIME_PERIOD"])
    frame["value"] = pd.to_numeric(frame["OBS_VALUE"], errors="coerce")
    # IMF SCALE is a power-of-ten multiplier. Absent SCALE means unscaled data.
    scale_field = "SCALE" if "SCALE" in frame else ("UNIT_MULT" if "UNIT_MULT" in frame else None)
    if scale_field:
        scale = pd.to_numeric(frame[scale_field], errors="coerce")
        reported = frame[scale_field].notna() & frame[scale_field].astype(str).str.strip().ne("")
        if (reported & scale.isna()).any():
            raise IMFDataError(f"Unrecognized IMF {scale_field} value; cannot determine units.")
        frame["value"] *= 10.0 ** scale.fillna(0)

    frame = frame.dropna(subset=["month", "value"])
    frame = frame[np.isfinite(frame["value"])].copy()
    frame = frame[frame["month"] >= EARLIEST_MONTH]
    if frame.empty:
        return pd.DataFrame(columns=["COUNTRY", "month", "value"])

    # Duplicate (country, month) values that disagree would make the result ambiguous.
    grouped = frame.groupby(["COUNTRY", "month"], sort=True)["value"]
    if (grouped.nunique() > 1).any():
        raise IMFDataError("IMF supplied conflicting values for a country and month.")
    return frame.drop_duplicates(subset=["COUNTRY", "month"])[["COUNTRY", "month", "value"]]


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_data(flow: str, codes: tuple[str, ...]) -> tuple[pd.DataFrame, str]:
    """Fetch all available months from 1976 for CPI or ER (two calls total)."""
    if flow not in {"CPI", "ER"}:
        raise ValueError("Only CPI and ER dataflows are supported")
    unique_codes = tuple(sorted(set(codes)))
    series_suffix = "CPI._T.IX.M" if flow == "CPI" else "XDC_USD.PA_RT.M"
    key = f"{'+'.join(unique_codes)}.{series_suffix}"
    last_error: IMFDataError | None = None
    for version in ("~", "+"):
        url = f"{BASE_URL}/data/dataflow/IMF.STA/{flow}/{version}/{key}"
        try:
            response = request_imf(
                url, params={"c[TIME_PERIOD]": "ge:1976-M01"}, accept="text/csv"
            )
            if response.status_code == 204 or not response.content.strip():
                return pd.DataFrame(columns=["COUNTRY", "month", "value"]), response.url
            parsed = parse_observations(response.content, unique_codes)
            return parsed, response.url
        except IMFDataError as exc:
            last_error = exc
    raise IMFDataError(f"IMF {flow} download failed. {last_error}")


def wide_series(data: pd.DataFrame, country: str) -> pd.Series:
    subset = data.loc[data["COUNTRY"] == country, ["month", "value"]]
    if subset.empty:
        return pd.Series(dtype="float64")
    return subset.set_index("month")["value"].astype("float64")


def compute_sample(
    months: pd.PeriodIndex,
    cpi: pd.DataFrame,
    fx: pd.DataFrame,
    code_1: str,
    code_2: str,
) -> tuple[pd.DataFrame, float | None, tuple[str, ...]]:
    """Inner arithmetic only for months with all four strictly positive inputs."""
    table = pd.DataFrame(index=months)
    table.index.name = "month"
    table["cpi_country_1"] = wide_series(cpi, code_1).reindex(months)
    table["cpi_country_2"] = wide_series(cpi, code_2).reindex(months)
    table["fx_country_1_per_usd"] = wide_series(fx, code_1).reindex(months)
    table["fx_country_2_per_usd"] = wide_series(fx, code_2).reindex(months)

    sources: list[str] = []
    for number, code in ((1, code_1), (2, code_2)):
        col = f"fx_country_{number}_per_usd"
        source_col = f"fx_country_{number}_source"
        table[source_col] = "IMF ER"
        # One USD necessarily equals one USD. This is a unit identity, not
        # an imputed IMF observation and is clearly disclosed in the export.
        if code == "USA":
            absent = table[col].isna()
            if absent.any():
                table.loc[absent, col] = 1.0
                table.loc[absent, source_col] = "USD/USD identity (not IMF observation)"
                sources.append(f"Country {number} USD/USD identity for {int(absent.sum())} month(s)")

    numeric = ["cpi_country_1", "cpi_country_2", "fx_country_1_per_usd", "fx_country_2_per_usd"]
    for column in numeric:
        table[column] = pd.to_numeric(table[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
    # A nominal exchange rate only requires both FX values. CPI gaps must not
    # suppress genuine nominal observations in the chart or export.
    fx_valid = table[["fx_country_1_per_usd", "fx_country_2_per_usd"]].gt(0).all(axis=1)
    s_name = "nominal_s_country_2_per_country_1"
    table[s_name] = np.nan
    table.loc[fx_valid, s_name] = (
        table.loc[fx_valid, "fx_country_2_per_usd"]
        / table.loc[fx_valid, "fx_country_1_per_usd"]
    )
    table[s_name] = table[s_name].replace([np.inf, -np.inf], np.nan)

    valid = table[numeric].gt(0).all(axis=1) & table[s_name].notna()
    table["rer"] = np.nan
    table.loc[valid, "rer"] = (
        table.loc[valid, s_name]
        * table.loc[valid, "cpi_country_1"]
        / table.loc[valid, "cpi_country_2"]
    )
    table["rer"] = table["rer"].replace([np.inf, -np.inf], np.nan)
    table["valid_rer_observation"] = valid & table["rer"].notna()
    table.loc[~table["valid_rer_observation"], "rer"] = np.nan
    rer_star = table["rer"].mean(skipna=True)
    mean = float(rer_star) if pd.notna(rer_star) and rer_star > 0 else None
    table["rer_sample_mean"] = mean if mean is not None else np.nan
    table["real_index"] = 100 * table["rer"] / mean if mean is not None else np.nan
    table = table.reset_index()
    table["month"] = table["month"].astype(str)
    return table[CSV_COLUMNS], mean, tuple(sources)


def draw_chart(table: pd.DataFrame, mode: str, code_1: str, code_2: str) -> go.Figure:
    time = pd.to_datetime(table["month"] + "-01")
    both = mode == "Both"
    fig = make_subplots(specs=[[{"secondary_y": both}]])
    if mode in ("Real", "Both"):
        fig.add_trace(
            go.Scatter(
                x=time, y=table["real_index"], mode="lines", name="Real index (mean = 100)",
                line={"color": "#176ca6", "width": 2},
                hovertemplate="%{x|%b %Y}<br>Real index: %{y:.3f}<extra></extra>",
                connectgaps=False,
            ),
            secondary_y=False,
        )
        fig.add_hline(y=100, line_dash="dot", line_color="gray", opacity=0.5)
    if mode in ("Nominal", "Both"):
        fig.add_trace(
            go.Scatter(
                x=time, y=table["nominal_s_country_2_per_country_1"],
                mode="lines", name=f"Nominal S ({code_2}/{code_1})",
                line={"color": "#df7e25", "width": 2},
                hovertemplate="%{x|%b %Y}<br>Nominal S: %{y:.6g}<extra></extra>",
                connectgaps=False,
            ),
            secondary_y=both,
        )
    primary_title = "Real index (sample mean = 100)" if mode != "Nominal" else f"Nominal S ({code_2} currency per {code_1} currency)"
    fig.update_yaxes(title_text=primary_title, secondary_y=False)
    if both:
        fig.update_yaxes(
            title_text=f"Nominal S ({code_2} currency per {code_1} currency)", secondary_y=True
        )
    fig.update_xaxes(title_text="Month")
    fig.update_layout(
        hovermode="x unified", legend={"orientation": "h", "y": 1.12, "x": 0},
        margin={"l": 20, "r": 25, "t": 65, "b": 20}, height=500,
    )
    return fig


def main() -> None:
    st.set_page_config(page_title="IMF Real Exchange Rate Explorer", page_icon="💱", layout="wide")
    st.title("💱 Real Exchange Rate Explorer")
    st.caption("Source: International Monetary Fund (IMF), monthly CPI and exchange rates | SDMX 3.0")

    with st.spinner("Loading country choices from IMF metadata…"):
        countries, country_issue = country_options()
    if country_issue:
        st.info(country_issue)

    names = sorted(countries, key=lambda code: (countries[code].casefold(), code))
    with st.sidebar:
        st.header("Choose countries and sample")
        country_1 = st.selectbox(
            "Country 1 (base currency)", names,
            index=names.index("GBR"),
            format_func=lambda c: f"{countries[c]} ({c})", key="country1",
        )
        country_2 = st.selectbox(
            "Country 2 (quoted currency)", names,
            index=names.index("USA"),
            format_func=lambda c: f"{countries[c]} ({c})", key="country2",
        )
        latest = pd.Period(date.today().strftime("%Y-%m"), freq="M")
        available_months = pd.period_range(EARLIEST_MONTH, latest, freq="M")
        labels = [str(p) for p in available_months]
        initial_start = "2010-01" if "2010-01" in labels else labels[0]
        start = st.selectbox("Start month", labels, index=labels.index(initial_start))
        end = st.selectbox("End month", labels, index=len(labels) - 1)
        mode = st.radio("Chart", ("Real", "Nominal", "Both"), index=2, horizontal=True)
        if st.button("Refresh IMF data", help="Clears cached IMF downloads and reloads data"):
            fetch_data.clear()
            country_options.clear()
            st.rerun()

    if pd.Period(start, freq="M") > pd.Period(end, freq="M"):
        st.error("Start month must be on or before end month.")
        st.stop()

    st.write(
        f"**Country 1:** {countries[country_1]} ({country_1}) · "
        f"**Country 2:** {countries[country_2]} ({country_2})"
    )
    st.latex(r"S_t=\frac{FX_{2,t}}{FX_{1,t}},\quad "
             r"RER_t=S_t\frac{P_{1,t}}{P_{2,t}},\quad "
             r"I_t=100\frac{RER_t}{\overline{RER}_{\mathrm{selected\ sample}}}")
    st.caption(
        "FX is national-currency units per USD. Nominal S is country-2 currency per "
        "one unit of country-1 currency. The RER sample average is recalculated every time "
        "the sample months change. Missing months are not filled or interpolated."
    )

    keys = tuple(sorted({country_1, country_2}))
    try:
        with st.spinner("Downloading live CPI and exchange-rate observations from the IMF…"):
            cpi, cpi_url = fetch_data("CPI", keys)
            fx, fx_url = fetch_data("ER", keys)
    except IMFDataError as exc:
        st.error(str(exc))
        st.info(
            "The IMF service may be temporarily offline or blocking requests. "
            "Retry or use Refresh IMF data after access is restored. No synthetic data is shown."
        )
        st.stop()

    months = pd.period_range(start, end, freq="M")
    table, rer_star, identity_notes = compute_sample(months, cpi, fx, country_1, country_2)
    valid_count = int(table["valid_rer_observation"].sum())
    total = len(table)
    if identity_notes:
        st.info("USD exchange-rate definition: " + "; ".join(identity_notes) + ".")
    missing = []
    for number, code in ((1, country_1), (2, country_2)):
        if cpi.loc[cpi["COUNTRY"] == code].empty:
            missing.append(f"No IMF monthly CPI data returned for {countries[code]} ({code}).")
        if fx.loc[fx["COUNTRY"] == code].empty and code != "USA":
            missing.append(f"No IMF monthly FX data returned for {countries[code]} ({code}).")
    if missing:
        st.warning(" ".join(missing))

    a, b, c = st.columns(3)
    a.metric("Valid RER months", f"{valid_count} / {total}")
    b.metric("RER* (selected sample mean)", f"{rer_star:,.6g}" if rer_star else "N/A")
    observed_mean = table["real_index"].mean(skipna=True)
    c.metric("Mean real index", f"{observed_mean:.6f}" if pd.notna(observed_mean) else "N/A")

    if rer_star is None:
        st.error(
            "No valid months have all four strictly positive CPI and FX inputs in this "
            "sample. Try another period or pair. The CSV below shows the missing inputs."
        )
    elif valid_count < total:
        st.warning(
            f"{total - valid_count} selected month(s) lack a complete positive data set "
            "and are excluded from the mean."
        )

    has_nominal = table["nominal_s_country_2_per_country_1"].notna().any()
    if rer_star is not None or (mode in ("Nominal", "Both") and has_nominal):
        st.plotly_chart(draw_chart(table, mode, country_1, country_2), use_container_width=True)
    elif not has_nominal:
        st.info("No valid nominal exchange-rate observations are available in this sample.")

    st.subheader("Observations and calculations")
    st.dataframe(table, hide_index=True, use_container_width=True)
    filename = f"imf_rer_{country_1}_{country_2}_{start}_{end}.csv"
    st.download_button(
        label="Download selected sample CSV",
        data=table.to_csv(index=False, na_rep=""),
        file_name=filename,
        mime="text/csv",
    )
    with st.expander("Source URLs, definitions, and data limitations"):
        st.markdown(
            "**Source:** [IMF CPI](https://data.imf.org/Datasets/CPI) and "
            "[IMF ER](https://data.imf.org/Datasets/ER) datasets via the "
            "[IMF SDMX API](https://data.imf.org/en/Resource-Pages/IMF-API). "
            "Data are cached for six hours; use Refresh IMF data to try a fresh download."
        )
        st.code(f"CPI: {cpi_url}\nER: {fx_url}", language="text")
        st.markdown(
            "**Series:** `{ISO3}.CPI._T.IX.M` and "
            "`{ISO3}.XDC_USD.PA_RT.M`; Euro area uses `G163`. "
            "`S = FX2/FX1`; `RER = S×CPI1/CPI2`; "
            "`RER* = mean(valid sample RER)`; `Real Index = 100×RER/RER*`. "
            "IMF `SCALE`, when supplied, is applied as a power of ten. "
            "The USD/USD identity equals 1 by definition and is identified separately "
            "in the CSV; no other values are imputed. CPI rebasing, historical currency "
            "changes, gaps, and non-overlapping country coverage can affect the result. "
            "An indexed RER does not measure a purchasing-power-parity exchange rate."
        )


if __name__ == "__main__":
    main()
