HTF Return Temperature Calculation Findings

Purpose

This document records the findings from reverse-engineering the Høje
Taastrup Fjernvarme (HTF) Selvbetjening return-temperature chart.

The purpose is to preserve the calculation logic and the relationship
between the historical portal data and the temperature values displayed
by HTF, so the information can be reused later when improving the Home
Assistant integration or dashboard.

> **Important:** This document records the findings available so far. It
> does not modify the integration code.

────────

1. HTF return-temperature page

The relevant HTF page is:

https://selvbetjening.htf.dk/returtemperatur/

The page displays a chart titled:

Din Returtemperatur vs Ønsket Max Returtemperatur

The chart contains:

• Din returtemperatur — the user’s return temperature
• God returtemperatur — the desired maximum return temperature
• For høj returtemperatur — the area above the desired maximum

The Y-axis is in degrees Celsius.

────────

2. Year-specific target temperature

The portal provides goodReturnTemperatureData.

Observed values:

Year     Desired maximum return temperature

────────

2023                                   43°C
2024                                   43°C
2025                                   42°C
2026                                   41°C

The target is therefore year-dependent.

It must not be hard-coded as 41°C for all historical years.

For example:

• A 40°C return temperature is below the 2025 limit of 42°C.
• A 40°C return temperature is below the 2026 limit of 41°C.
• A 42.5°C return temperature would be above the 2026 limit.

────────

3. Important discovery: historical raw values are not the displayed temperature

The historical portal JSON contains several meter counters.

Relevant counters observed on the return-temperature page include:

Counter Type    Unit

────────

      2 FV-M3   M3
      4 FV-FT   m3xC
      5 FV-RT   m3xC

The raw values from these counters should not automatically be plotted
as °C.

For example, the historical portal data contains:

October 2026

• One relevant raw value: 0.30
• Another relevant raw value: 9
• HTF chart: 30.00°C

The relationship is:

9 / 0.30 = 30.00°C

August 2026

• One relevant raw value: 6.29
• Another relevant raw value: 198
• HTF chart: 31.48°C

The relationship is:

198 / 6.29 = 31.48°C

Therefore:

> The displayed return temperature is derived from a ratio between the
> appropriate paired meter values.

Simply plotting the raw FV-RT values is incorrect.

────────

4. Confirmed screenshot values

The HTF portal screenshots provide the following displayed
return-temperature values.

Month        Year   HTF displayed return temperature   Desired maximum

────────

Mar 2025     2025                            39.01°C           42.00°C
May 2025     2025                            29.02°C           42.00°C
Aug 2025     2025                            30.95°C           42.00°C
Nov 2025     2025                            37.84°C           42.00°C
Aug 2026     2026                            31.48°C           41.00°C
Oct 2026     2026                            30.00°C           41.00°C

These values are useful as validation points for any future
implementation.

────────

5. Validation examples

August 2026

Raw paired values:

6.29
198

Calculation:

198 / 6.29 = 31.4785...

Rounded to two decimals:

31.48°C

This exactly matches the HTF chart.

October 2026

Raw paired values:

0.30
9

Calculation:

9 / 0.30 = 30.00

HTF chart:

30.00°C

This also matches exactly.

────────

6. 2025 validation points

The following HTF values were read directly from screenshots:

March 2025

HTF: 39.01°C
Target: 42.00°C

May 2025

HTF: 29.02°C
Target: 42.00°C

August 2025

HTF: 30.95°C
Target: 42.00°C

November 2025

HTF: 37.84°C
Target: 42.00°C

These should be used as regression/validation values when reconstructing
the historical calculation.

────────

7. Important distinction: target vs calculation

The yearly goodReturnTemperatureData values are thresholds, not
conversion factors.

They define the boundary between:

Normal / Good return temperature
        |
        |  target
        v
-------------------------
        |
        |  Too high

For example:

2025 target = 42°C
2026 target = 41°C

The target should therefore be plotted independently from the calculated
return-temperature series.

────────

8. Frontend processing

The HTF frontend uses three relevant counters:

let counterNumberOfM3 = 2;
let counterNumberOfFlowTemperature = 4;
let counterNumberOfReturnTemperature = 5;

It constructs metersV2 and keeps dates common to the relevant meter
data before creating the return-temperature chart.

The chart calculation then uses the transformed data arrays and performs
a ratio between the relevant return-temperature and M3 series.

Conceptually:

paired return-related value
---------------------------
paired M3-related value
        =
return temperature °C

The important point is that the values used in the final ratio are the
appropriate paired/transformed meter values, not necessarily the raw
column that happens to be labelled FV-RT in the stored historical
JSON.

────────

9. What not to do

Do not plot raw FV-RT directly as °C

For example:

FV-RT = 0.30

must not automatically become:

0.30°C

The HTF chart displays:

30.00°C

for the corresponding October 2026 point.

Do not use one fixed yearly target

Do not use:

41°C for every year

The portal provides different targets by year.

Do not apply an arbitrary multiplier

The historical values must be derived from the appropriate paired meter
values rather than applying a guessed constant multiplier to raw FV-RT.

────────

10. Home Assistant dashboard implication

For a future Home Assistant dashboard, the desired presentation is:

• Historical return-temperature line in °C
• Two decimal places where appropriate
• Year-specific HTF target line
• Green area below the target
• Red area above the target
• The return-temperature line should visually indicate when it exceeds
the year’s target

Example:

2026 target = 41°C

50°C       RED / TOO HIGH
           |
41°C  ----- TARGET ----------------
           |
           GREEN / GOOD
30°C  -----●

The target must change automatically according to the year of each
historical point.

────────

11. Known reference values for future testing

Use these values as regression tests:

2025-03 -> 39.01°C
2025-05 -> 29.02°C
2025-08 -> 30.95°C
2025-11 -> 37.84°C

2026-08 -> 31.48°C
2026-10 -> 30.00°C

Expected yearly targets:

2025 -> 42.00°C
2026 -> 41.00°C

If a future implementation produces substantially different values for
these points, the calculation or meter pairing should be investigated
before changing the dashboard.

────────

12. Current conclusion

The most important finding is:

> **The HTF historical return-temperature table contains raw meter
> values, while the HTF website’s return-temperature chart displays a
> calculated temperature derived from paired meter values.**

The target temperature is independently supplied by
goodReturnTemperatureData and varies by year.

The confirmed examples demonstrate that the final temperature can be
reproduced from the correct paired values:

August 2026:
198 / 6.29 = 31.48°C

October 2026:
9 / 0.30 = 30.00°C

These findings should be retained as reference material for future work
on the HTF Home Assistant integration and dashboard.