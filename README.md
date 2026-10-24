# IMF Real Exchange Rate Explorer

A Python 3.12 / [Streamlit](https://streamlit.io/) app that downloads **live monthly CPI and exchange-rate series from the IMF SDMX 3.0 API**, lets a user select two economies (including the Euro area), plots real and nominal exchange rates, and exports a sample-specific CSV.

## Source and series

- API: `https://api.imf.org/external/sdmx/3.0`
- IMF Statistics Department (IMF.STA), **CPI** dataflow, key: `{ISO3}.CPI._T.IX.M`
- IMF Statistics Department (IMF.STA), **ER** dataflow, key: `{ISO3}.XDC_USD.PA_RT.M`
- `G163` selects the Euro area where IMF data exist.
- Data are fetched from January 1976 onward **where available**, using the SDMX `/data/dataflow/IMF.STA/{CPI|ER}/~/{KEY}` endpoint and monthly time constraint.
- Monthly time values such as `2024-M01` and `2024-01` are supported.
- Country names are obtained from the IMF's `IMF/CL_COUNTRY` codelist. If its metadata service is down, the app uses an ISO-3 country/territory list as a fallback and warns that not all options necessarily have IMF data.

Sources: [IMF CPI](https://data.imf.org/Datasets/CPI), [IMF Exchange Rates](https://data.imf.org/Datasets/ER), [IMF API](https://data.imf.org/en/Resource-Pages/IMF-API).

## Definitions

For countries 1 and 2, let `FX1` and `FX2` be IMF monthly **local-currency units per U.S. dollar** and `P1` and `P2` their CPI index levels.

```text
S(t)          = FX2(t) / FX1(t)
RER(t)        = S(t) * P1(t) / P2(t)
RER*          = mean of valid RER(t) in the user-selected sample
Real Index(t) = 100 * RER(t) / RER*
```

`S` is units of **country 2** currency per one unit of **country 1** currency. For UK (country 1) and USA (country 2), `S` is **USD per GBP**, not GBP per USD. Nominal `S` is **not indexed**. In **Both** mode, nominal and real use separate y-axes.

`RER*` changes whenever the start or end month changes. The mean Real Index across valid months of the selected sample should be 100 (within floating-point tolerance). Monthly observations with missing, nonfinite or nonpositive inputs are excluded from RER computations and the mean, without forward-filling or interpolation. The downloaded CSV includes *all selected calendar months*, CPI and FX inputs (adjusted for any IMF SCALE), the FX source, nominal `S`, `RER`, `RER*`, the Real Index, and a valid-month flag.

**USD note:** If the IMF does not publish `USA.XDC_USD.PA_RT.M`, the app uses `1 USD per USD` as a mathematically exact currency identity, not a fabricated IMF observation. The CSV explicitly marks each such value `USD/USD identity (not IMF observation)`; it never substitutes values for any other missing country or month.

**Caveats:** CPI indices can have different base years and historical currency changes; the indexed RER is a relative time-series indicator, not a PPP price level. The IMF may not publish both required series for every country, particularly for early years, territories, or the Euro area. Historic figures may be revised.

## Run locally (optional)

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

Python 3.12 is recommended. **No local Python installation is required** to deploy via GitHub and Streamlit Community Cloud.

## Deploy without local Python

1. On [github.com/new](https://github.com/new), create a **Public** repository called `real-exchange-rate-app` with a README and default `main` branch.
2. In the GitHub web UI, create a top-level `app.py` file and paste the entire code (without triple backticks). Commit changes.
3. Create a top-level `requirements.txt` file with the dependency lines. Commit changes.
4. Replace the top-level `README.md` with this README. Commit changes.
5. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub. Choose **Create app → Yup, I have an app**.
6. Select your repository, branch `main`, entrypoint `app.py`, and **Python 3.12** in Advanced settings. Leave Secrets blank. Click **Deploy**.
7. Save the public `https://YOUR-APP-NAME.streamlit.app` URL and your GitHub repository URL.

If deployment errors occur, inspect Streamlit Cloud **Manage app → Logs**, copy the complete traceback, and correct the files in GitHub. The IMF API can be unavailable or rate-limited; failures display errors instead of fake observations. No API key is normally required.

## Assignment test: UK vs USA

1. Select **United Kingdom (GBR)** as Country 1, **United States (USA)** as Country 2. Confirm S is USD/GBP.
2. Select a period that returns valid monthly observations; view **Real**, **Nominal**, and **Both** charts. In Both, inspect both y-axes.
3. Download CSV. Pick one valid month and calculate `S = FX2/FX1`, `RER = S*CPI1/CPI2` from exported values. Check your result against the `rer` column.
4. Calculate the arithmetic average of nonmissing `real_index` values in the CSV: it should be 100 (rounding aside). Change the sample dates and repeat; `rer_sample_mean` should also update.
5. Include the app link, GitHub link, model name/date and prompt screenshots, a real-data manual verification, and the CSV in your course submission.

**Worked illustration, not IMF observations:** If the UK rate is 0.80 GBP/USD, the US rate is 1 USD/USD, UK CPI 120, and US CPI 125, then `S=1/0.80=1.25 USD/GBP` and `RER=1.25*(120/125)=1.20`.

## Troubleshooting

- **Missing columns / invalid response:** IMF sometimes changes API output or returns HTML on maintenance; expand the data-source section for query URLs.
- **No valid months:** Select another sample or country pair. The CSV retains empty months so you can inspect which series are missing.
- **HTTP 403 / 429 / 5xx:** IMF access restrictions, throttling, or maintenance; try a fresh request later. Do not invent replacement observations.
- **Euro area:** Its `G163` CPI or ER coverage may be unavailable or shorter than national series; the app reports missing data rather than manufacturing rates.
- **Invalid country availability:** The IMF metadata country list includes codes with no observations; the app explains absent series.
