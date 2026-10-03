### The Core Verdict

The script is well-conceived in its econometrics: using **QLIKE** instead of MSE is the standard approach in volatility forecasting literature (Patton, 2011), running **Mincer-Zarnowitz regressions** to extract empirical shrinkage factors is standard practice, and enforcing strict point-in-time boundaries eliminates lookahead bias.

However, the diagnostic contains **structural flaws, Jensen’s inequality scale corruptions, and statistical leakage** that directly explain why GARCH and EWMA showed identical zero-skill rank ICs (-0.064 vs -0.065) and why the diagnostic over-forecasts by roughly 10%.

---

### Critical Issues & Strict Critique

#### 1. Jensen's Inequality Distortion: Averaging Standard Deviations Instead of Variances

In `build_panel()` and `summarise()`:

```python
realised = float(np.std(fut, ddof=1) * np.sqrt(TRADING_DAYS) * 100) #[cite: 1]
# ...
f, a = forecast[m] ** 2, realised[m] ** 2  # Inside qlike[cite: 1]

```

* **The Flaw:** By taking the sample standard deviation $\hat{\sigma}$ first and then squaring it later inside `qlike`, you introduce negative bias due to Jensen's inequality:



$$\mathbb{E}[\hat{\sigma}] \le \sqrt{\mathbb{E}[\hat{\sigma}^2]}$$



More critically, when you run Mincer-Zarnowitz regressions ($a = \alpha + \beta f + \epsilon$) on **annualized volatility (%)** rather than **annualized variance ($\%^2$)**, the regression is mathematically misspecified. Volatility additive errors are non-Gaussian and heteroskedastic.


* **The Fix:** The target variable $y_t$ must be **Integrated Variance (Realized Variance)** over the forward horizon $h$:

$$RV_{t, t+h} = \sum_{k=1}^h r_{t+k}^2 \times \frac{252}{h}$$



Run the MZ regression on **variance**, then take the square root of the fitted shrunk forecast only when computing dollar option payoffs.

#### 2. The Target Variable is Inefficient (Close-to-Close vs. High-Frequency / Range)

* **The Flaw:** The target `realised` uses 20 daily close-to-close returns:


```python
realised = float(np.std(fut, ddof=1) * np.sqrt(TRADING_DAYS) * 100) #[cite: 1]

```


A 20-day sample standard deviation has an enormous standard error ($\approx \frac{\sigma}{\sqrt{2N}} \approx \frac{\sigma}{\sqrt{40}} \approx 16\%$).
* **Consequence:** Your "ground truth" target contains massive measurement noise. When you evaluate an advanced model (GARCH or EWMA) against a noisy realized proxy via QLIKE, the noise in the evaluation target degrades the scoring ability of the loss function, causing GARCH to appear no better than static trailing HV20.


* **The Fix:** Measure forward realized variance using **Yang-Zhang or Garman-Klass daily summations** over the forward $h$ days:

$$RV_{t, t+h} = \frac{252}{h} \sum_{k=1}^h \sigma^2_{\text{YZ}, t+k}$$



This reduces evaluation target noise by over 70%, allowing GARCH's dynamic parameters to demonstrate their statistical edge.

#### 3. Overlapping Observations in Panel Summaries (Inflated $t$-Stats / Invalid $R^2$)

* **The Flaw:** When `--sample every` or overlapping calendar runs are executed, forward windows overlap:

$$\text{Target}_t = \text{Var}(r_{t \to t+20}), \quad \text{Target}_{t+5} = \text{Var}(r_{t+5 \to t+25})$$



Because 15 trading days are shared between consecutive samples, the regression residuals $\epsilon_t$ and $\epsilon_{t+5}$ are autocorrelated (an MA(3) to MA(4) process).
* **Consequence:** The standard OLS estimator in `mz()` underestimates parameter standard errors, produces an artificially high $R^2$, and miscalculates the true confidence intervals of the shrinkage $\beta$.


* **The Fix:** Compute **Hansen-Hodrick or Newey-West adjusted standard errors** with lag $L = h - 1$, or strictly enforce non-overlapping windows (`args.every >= args.horizon`) when fitting the MZ shrinkage parameters.

#### 4. The "Excluding Event Days" Variance Leak

In `build_panel()`:

```python
mask = np.array([(sym, dd) not in events for dd in fut_dates]) #[cite: 1]
realised_ex = (float(np.std(fut[mask], ddof=1) * np.sqrt(TRADING_DAYS) * 100) #[cite: 1]
               if mask.sum() > 2 and mask.sum() >= h - 3 else np.nan) #[cite: 1]

```

* **The Flaw:** When an event day occurs at $t+5$, you simply slice it out and calculate `np.std()` on the remaining $h-1$ observations scaled by $\sqrt{252}$.


* **Consequence:** You are treating the return from day $t+4$ to $t+6$ as if the event never happened, but the price level has shifted. Slicing out a single day's log-return from a continuous time series corrupts the variance denominator and produces an artificially low volatility estimate.
* **The Fix:** If you exclude an earnings shock, you must replace the event return with the **median non-event daily variance** of that stock, or model the jump separately using an additive jump diffusion term:

$$\sigma^2_{\text{total}} = \sigma^2_{\text{diffusive}} + \frac{1}{h} J^2_{\text{earnings}}$$



#### 5. Survivorship and Symbol Coverage Bias

* **The Flaw:**
```python
cov = iv.groupby("symbol").size().sort_values(ascending=False) #[cite: 1]
symbols = [s for s in cov.index if s in set(symbols)][:args.max_symbols] #[cite: 1]

```


Selecting the highest-coverage symbols over a 5-year retrospective database (2020–2026) creates **survivorship bias**. Stocks that suffered massive volatility expansions, structural breakdowns, or were removed from the F&O segment (e.g., small/mid-cap names kicked out of F&O lists) have low coverage and are systematically omitted.


* **Consequence:** Your diagnostic evaluates predominantly large, liquid, low-tail-risk equities, causing the MZ $\beta$ shrinkage factor to under-protect against tail blowouts in volatile names.



---

### Concrete Code Modifications

#### 1. Corrected Mincer-Zarnowitz on Variance (with Robust Errors)

Replace `mz()` with an estimator that acts directly on variance and handles the OLS regression on clean scales:

```python
def mz_variance(forecast_vol: np.ndarray, realised_vol: np.ndarray) -> dict:
    """
    Mincer-Zarnowitz regression strictly executed in VARIANCE space.
    Realised_Var = alpha + beta * Forecast_Var + epsilon
    """
    m = np.isfinite(forecast_vol) & np.isfinite(realised_vol) & (forecast_vol > 0) & (realised_vol > 0)
    if np.sum(m) < 30:
        return {"n": int(np.sum(m)), "alpha": np.nan, "beta": np.nan, "r2": np.nan}
    
    # Square to convert Vol (%) -> Variance (%^2)
    f_var = (forecast_vol[m] ** 2)
    y_var = (realised_vol[m] ** 2)
    
    X = np.column_stack([np.ones_like(f_var), f_var])
    coef, *_ = np.linalg.lstsq(X, y_var, rcond=None)
    
    pred = X @ coef
    ss_res = float(np.sum((y_var - pred) ** 2))
    ss_tot = float(np.sum((y_var - y_var.mean()) ** 2))
    
    return {
        "n": int(len(f_var)),
        "alpha": float(coef[0]),
        "beta": float(coef[1]),
        "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    }

```

#### 2. High-Efficiency Forward Realized Target

In `build_panel()`, replace the 20-day close-to-close standard deviation with a rolling Yang-Zhang or Garman-Klass summation over the forward window:

```python
# Instead of: np.std(fut, ddof=1) * np.sqrt(TRADING_DAYS) * 100
# Compute forward realized variance via intraday bounds:
log_hl = np.log(fut_high / fut_low) ** 2
log_co = np.log(fut_close / fut_open) ** 2
daily_gk_var = 0.5 * log_hl - (2.0 * np.log(2.0) - 1.0) * log_co

# Integrated forward annualized realized volatility (%)
realised_gk = float(np.sqrt(daily_gk_var.mean() * TRADING_DAYS) * 100.0)

```

---

### Key Actionable Steps

1. **Change the Optimization Target:** Never run `mz()` or `qlike()` on standard deviation. Convert all inputs to variance ($\sigma^2$), run the diagnostics, calculate shrinkage $\beta$, and apply the square root at the very end.


2. **Upgrade the Realized Ground Truth:** Use Garman-Klass or Yang-Zhang for the evaluation window instead of 20 closing bars.
3. **Decouple Event Jump Risk from Diffusive RV:** Do not mask out earnings days. Measure the pure non-earnings baseline RV using the model, and evaluate earnings separately as an explicit discrete jump parameter ($J_{\text{event}}$) added to the straddle breakeven calculation.


This script presents a conceptually sound premise—using QLIKE and Mincer-Zarnowitz to evaluate forecast quality objectively and strictly separating the point-in-time training and testing horizons. However, there are significant statistical flaws and architectural inconsistencies between your diagnostic script and your core library that are likely corrupting the results.

Here is a strict critique of the issues and how to address them:

### 1. Jensen's Inequality and the Variance vs. Volatility Mismatch

**The Issue:**
In `rv_diagnostics.py`, the target realized metric is computed as a standard deviation: `realised = float(np.std(fut, ddof=1) * np.sqrt(TRADING_DAYS) * 100)`. Later, inside the `qlike` function, you square this value to convert it to variance: `f, a = forecast[m] ** 2, realised[m] ** 2`.
By taking the standard deviation first and squaring it later, you introduce a downward bias due to Jensen's Inequality ($[E(X)]^2 \le E[X^2]$).
Furthermore, your `mz` (Mincer-Zarnowitz) function runs ordinary least squares (OLS) regression directly on the volatility (`f, a = forecast[m], realised[m]`). Volatility errors are highly heteroskedastic; Mincer-Zarnowitz is mathematically designed to be run on *variance*.

**The Fix:**

* Calculate the target strictly as Realized Variance: sum the squared daily returns over the horizon, scale to annualized variance, and only apply the square root at the very end of the pipeline if you need a volatility figure for display.
* Run the Mincer-Zarnowitz regression on variance, not volatility.

### 2. The Event Exclusion Logic Corrupts the Variance Sum

**The Issue:**
To test if event days cause bias, `build_panel` excludes event dates using a boolean mask (`mask = np.array([(sym, dd) not in events for dd in fut_dates])`) and calculates the remaining standard deviation: `realised_ex = float(np.std(fut[mask], ddof=1) * np.sqrt(TRADING_DAYS) * 100)`.
If an earnings gap causes a $5\%$ price jump, simply dropping that day's log-return from the array and running `np.std()` on the remaining $h-1$ days treats the timeline as if the jump never happened, mathematically disconnecting the start and end prices of the option's lifecycle.

**The Fix:**
If the goal is to evaluate the model's ability to forecast the *diffusive* background variance, calculate total realized variance, isolate the squared return of the event day, and subtract it. Do not simply slice the array and recalculate standard deviation on the remaining elements.

### 3. Autocorrelation from Overlapping Windows

**The Issue:**
The script defines a default `horizon=20` (trading days ahead) and an `every=5` parameter for sampling. When the `--sample every` argument is used, you are taking overlapping 20-day windows spaced 5 days apart.
This creates a severe moving-average autocorrelation (specifically MA(3)) in the regression residuals of the Mincer-Zarnowitz test. Standard OLS underestimates standard errors under overlapping data, leading to artificially inflated $R^2$ values and invalid confidence in the $\beta$ shrinkage factor.

**The Fix:**
Either strictly enforce non-overlapping windows (where `every` $\ge$ `horizon`) when generating data for the `mz` regression, or implement Hansen-Hodrick or Newey-West robust standard errors to correct for the overlapping lags.

### 4. Code Duplication and API Inconsistency

**The Issue:**
`rv_diagnostics.py` manually re-defines `mz()` and `qlike()` at the top of the file, despite `straddle_core.py` already containing `mincer_zarnowitz()` and `qlike()` functions.
Critically, these functions do not behave the same way. The `qlike` in `straddle_core.py` expects variance inputs directly (`qlike(forecast_var, realised_var)`), whereas `rv_diagnostics.py` expects volatility inputs and squares them internally (`f, a = forecast[m] ** 2, realised[m] ** 2`). This divergence guarantees bugs if the diagnostic layer and core simulation layer are ever mixed.

**The Fix:**
Delete the redundant metric functions in `rv_diagnostics.py` and import `mincer_zarnowitz` and `qlike` directly from `straddle_core.py`. Ensure that `rv_diagnostics.py` passes true variance to these functions to maintain parity with the core modeling engine.

### 5. Close-to-Close Noise Penalty

**The Issue:**
Using a 20-day sample standard deviation (`np.std`) of close-to-close log returns as your "ground truth" introduces massive estimator noise. When evaluating sophisticated models like GARCH against a highly noisy proxy, the QLIKE loss function struggles to discern actual forecast skill, which heavily contributes to your observation that GARCH and EWMA show near-identical rank ICs.

**The Fix:**
Upgrade the actual realized target calculation in `build_panel` from simple close-to-close standard deviation to a high-efficiency intra-day estimator (like Parkinson or Garman-Klass) over the forward $h$-day window.