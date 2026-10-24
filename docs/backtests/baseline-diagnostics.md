# Why the baseline pool loses (task 3.8 diagnostics)

**Run:** 2026-10-24 18:29 UTC with `uv run python -m backtest.diagnostics`, risk.yaml `0020a669e66e`, regime `cu+2_corr-2_c2_dist8`, 370 s. Generated; only the Reading at the end is written by hand. What is computed and how it is read were fixed first: docs/backtests/baseline-diagnostics-preregistration.md. **Tuning period only (2017-2022)**; the holdout stays closed.

Closed trades in the reference pool: 4,154, expectancy -0.10.

## D1: costs

The pool re-played through `sim/` with every slippage tier at 0, against the reference.

| Strategy | Reference (R) | Zero cost (R) | Difference | 90% interval | Zero-cost gate interval |
|---|---|---|---|---|---|
| **All** | -0.10 (4154) | +0.03 (4156) | +0.13 | +0.12 to +0.14 | -0.04 to +0.11 |
| `VCP_BREAKOUT` | -0.03 (1023) | +0.08 (1023) | +0.11 | +0.10 to +0.12 | -0.03 to +0.18 |
| `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.34 (130) | +0.17 | +0.13 to +0.21 | -0.53 to -0.12 |
| `EPISODIC_PIVOT` | +0.23 (747) | +0.43 (747) | +0.20 | +0.17 to +0.24 | +0.16 to +0.78 |
| `LEADER_PULLBACK` | -0.22 (1500) | -0.09 (1502) | +0.13 | +0.12 to +0.15 | -0.18 to -0.00 |
| `EMERGING_LEADER` | -0.21 (754) | -0.13 (754) | +0.08 | +0.07 to +0.08 | -0.20 to -0.06 |

## D2-D7: the splits

Each split gives the buckets over all strategies, each strategy's expectancy by bucket, and the reading of rule 2: the best bucket against the worst (buckets of 30 trades or more), its 90% interval, and the years the best was ahead (of those with 10+ trades in both). Informative = the interval excludes zero and the best was ahead in 4+ years.

**Informative:** D2 chase above the pivot (all); D2 chase above the pivot (`EPISODIC_PIVOT`); D3 initial stop width (all); D3 initial stop width (`LEADER_PULLBACK`); D4 rank in the capped list (all); D4 rank in the capped list (`EPISODIC_PIVOT`); D4 conviction (`EPISODIC_PIVOT`); D5 regime score the evening before (all); D5 regime score the evening before (`VCP_BREAKOUT`); D5 regime score the evening before (`LEADER_PULLBACK`); D6 entry price (all); D6 entry price (`EPISODIC_PIVOT`); D6 entry price (`LEADER_PULLBACK`); D7 RS rating (`EPISODIC_PIVOT`); D7 extension above the 50-day (all); D7 extension above the 50-day (`EPISODIC_PIVOT`); D7 ADR (all); D7 ADR (`VCP_BREAKOUT`); D7 ADR (`EPISODIC_PIVOT`); D7 EPS growth (`VCP_BREAKOUT`).

### D2: chase above the pivot

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `<= 0.5%` | 2869 | -0.12 | 29% | +0.03 (781) | -0.43 (497) | -0.05 (616) | +0.46 (308) | -0.38 (405) | -0.47 (262) |
| `0.5-1.5%` | 723 | -0.22 | 27% | +0.03 (184) | -0.34 (105) | -0.22 (158) | -0.16 (101) | -0.41 (117) | -0.50 (58) |
| `1.5-3%` | 263 | +0.24 | 30% | +0.81 (60) | -0.37 (27) | -0.05 (45) | +0.62 (58) | -0.06 (58) | -0.40 (15) |
| `> 3%` | 299 | +0.10 | 29% | +0.14 (35) | -0.28 (26) | +0.21 (33) | +0.01 (73) | +0.21 (95) | +0.12 (37) |

| Strategy | `<= 0.5%` | `0.5-1.5%` | `1.5-3%` | `> 3%` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | -0.04 (884) | +0.03 (113) | +0.01 (20) | -0.55 (6) |
| `MOMENTUM_BREAKOUT` | -0.45 (104) | -0.88 (12) | -0.50 (8) | -0.71 (6) |
| `EPISODIC_PIVOT` | +0.79 (148) | -0.27 (155) | +0.35 (167) | +0.13 (277) |
| `LEADER_PULLBACK` | -0.24 (1130) | -0.24 (301) | +0.09 (59) | +0.30 (10) |
| `EMERGING_LEADER` | -0.20 (603) | -0.26 (142) | +0.29 (9) | - |

- All: best `1.5-3%` vs worst `0.5-1.5%` +0.46 (+0.17 to +0.80); best ahead in 5 of 6 years -> **informative**.
- `VCP_BREAKOUT`: best `0.5-1.5%` vs worst `<= 0.5%` +0.06 (-0.24 to +0.42); best ahead in 2 of 5 years -> not informative.
- `EPISODIC_PIVOT`: best `<= 0.5%` vs worst `0.5-1.5%` +1.06 (+0.09 to +2.37); best ahead in 4 of 6 years -> **informative**.
- `LEADER_PULLBACK`: best `1.5-3%` vs worst `<= 0.5%` +0.33 (-0.18 to +0.98); best ahead in 2 of 3 years -> not informative.
- `EMERGING_LEADER`: best `<= 0.5%` vs worst `0.5-1.5%` +0.06 (-0.11 to +0.22); best ahead in 2 of 3 years -> not informative.

### D3: initial stop width

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< 3%` | 1279 | -0.18 | 24% | +0.01 (465) | -0.68 (162) | -0.27 (343) | +0.77 (104) | -0.48 (133) | -0.72 (72) |
| `3-5%` | 1655 | -0.10 | 31% | +0.12 (438) | -0.38 (310) | +0.01 (324) | +0.21 (205) | -0.35 (246) | -0.46 (132) |
| `5-7%` | 963 | -0.07 | 30% | +0.18 (134) | -0.20 (160) | +0.06 (170) | +0.08 (158) | -0.27 (199) | -0.21 (142) |
| `>= 7%` | 257 | +0.17 | 31% | +0.20 (23) | -0.48 (23) | +0.98 (15) | +0.40 (73) | +0.18 (97) | -0.42 (26) |

| Strategy | `< 3%` | `3-5%` | `5-7%` | `>= 7%` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | +0.12 (231) | -0.04 (530) | -0.15 (249) | -0.13 (13) |
| `MOMENTUM_BREAKOUT` | -0.68 (64) | -0.63 (38) | -0.47 (14) | +0.58 (14) |
| `EPISODIC_PIVOT` | +0.31 (210) | +0.33 (245) | -0.09 (165) | +0.31 (127) |
| `LEADER_PULLBACK` | -0.36 (702) | -0.23 (503) | +0.14 (202) | +0.03 (93) |
| `EMERGING_LEADER` | -0.41 (72) | -0.24 (339) | -0.12 (333) | -0.48 (10) |

- All: best `>= 7%` vs worst `< 3%` +0.35 (+0.03 to +0.68); best ahead in 5 of 6 years -> **informative**.
- `VCP_BREAKOUT`: best `< 3%` vs worst `5-7%` +0.26 (-0.02 to +0.56); best ahead in 3 of 5 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `3-5%` vs worst `< 3%` +0.05 (-0.32 to +0.44); best ahead in 1 of 1 years -> not informative.
- `EPISODIC_PIVOT`: best `3-5%` vs worst `5-7%` +0.42 (+0.09 to +0.79); best ahead in 3 of 6 years -> not informative.
- `LEADER_PULLBACK`: best `5-7%` vs worst `< 3%` +0.50 (+0.21 to +0.80); best ahead in 6 of 6 years -> **informative**.
- `EMERGING_LEADER`: best `5-7%` vs worst `< 3%` +0.29 (+0.07 to +0.49); best ahead in 2 of 2 years -> not informative.

### D4: rank in the capped list

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `1-10` | 1641 | +0.03 | 29% | +0.19 (369) | -0.34 (253) | +0.05 (306) | +0.49 (242) | +0.01 (310) | -0.42 (161) |
| `11-20` | 1050 | -0.23 | 27% | +0.03 (263) | -0.44 (178) | -0.12 (218) | -0.05 (126) | -0.50 (165) | -0.57 (100) |
| `21-40` | 1463 | -0.16 | 29% | +0.02 (428) | -0.47 (224) | -0.16 (328) | +0.30 (172) | -0.54 (200) | -0.27 (111) |

| Strategy | `1-10` | `11-20` | `21-40` |
|---|---|---|---|
| `VCP_BREAKOUT` | +0.03 (353) | -0.09 (301) | -0.04 (369) |
| `MOMENTUM_BREAKOUT` | -0.44 (107) | -0.78 (21) | -1.30 (2) |
| `EPISODIC_PIVOT` | +0.33 (604) | -0.18 (121) | -0.39 (22) |
| `LEADER_PULLBACK` | -0.16 (325) | -0.29 (374) | -0.22 (801) |
| `EMERGING_LEADER` | -0.23 (252) | -0.28 (233) | -0.12 (269) |

- All: best `1-10` vs worst `11-20` +0.26 (+0.11 to +0.42); best ahead in 6 of 6 years -> **informative**.
- `VCP_BREAKOUT`: best `1-10` vs worst `11-20` +0.12 (-0.10 to +0.35); best ahead in 4 of 5 years -> not informative.
- `EPISODIC_PIVOT`: best `1-10` vs worst `11-20` +0.51 (+0.06 to +0.96); best ahead in 5 of 5 years -> **informative**.
- `LEADER_PULLBACK`: best `1-10` vs worst `11-20` +0.13 (-0.07 to +0.34); best ahead in 6 of 6 years -> not informative.
- `EMERGING_LEADER`: best `21-40` vs worst `11-20` +0.16 (+0.01 to +0.32); best ahead in 3 of 3 years -> not informative.

### D4: conviction

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `top quarter` | 1344 | -0.00 | 29% | +0.09 (312) | -0.40 (217) | -0.04 (233) | +0.67 (186) | +0.00 (271) | -0.51 (125) |
| `the rest` | 2810 | -0.15 | 29% | +0.08 (748) | -0.41 (438) | -0.09 (619) | +0.11 (354) | -0.46 (404) | -0.36 (247) |

| Strategy | `top quarter` | `the rest` |
|---|---|---|
| `VCP_BREAKOUT` | -0.00 (142) | -0.04 (881) |
| `MOMENTUM_BREAKOUT` | -0.93 (44) | -0.29 (86) |
| `EPISODIC_PIVOT` | +1.19 (231) | -0.20 (516) |
| `LEADER_PULLBACK` | -0.26 (690) | -0.19 (810) |
| `EMERGING_LEADER` | -0.25 (237) | -0.19 (517) |

- All: best `top quarter` vs worst `the rest` +0.14 (-0.00 to +0.31); best ahead in 5 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `top quarter` vs worst `the rest` +0.03 (-0.22 to +0.29); best ahead in 2 of 5 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `the rest` vs worst `top quarter` +0.64 (+0.32 to +0.96); best ahead in 1 of 1 years -> not informative.
- `EPISODIC_PIVOT`: best `top quarter` vs worst `the rest` +1.39 (+0.69 to +2.35); best ahead in 6 of 6 years -> **informative**.
- `LEADER_PULLBACK`: best `the rest` vs worst `top quarter` +0.07 (-0.09 to +0.22); best ahead in 2 of 6 years -> not informative.
- `EMERGING_LEADER`: best `the rest` vs worst `top quarter` +0.06 (-0.06 to +0.18); best ahead in 2 of 3 years -> not informative.

### D5: regime score the evening before

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `<= +2` | 1540 | -0.03 | 31% | +0.22 (162) | -0.25 (345) | +0.18 (301) | +1.02 (167) | -0.35 (268) | -0.43 (297) |
| `+3` | 1286 | -0.05 | 29% | +0.22 (360) | -0.51 (118) | -0.10 (248) | +0.03 (261) | -0.21 (274) | -0.24 (25) |
| `+4` | 981 | -0.19 | 27% | -0.07 (425) | -0.56 (88) | -0.25 (234) | -0.11 (62) | -0.20 (122) | -0.42 (50) |
| `>= +5` | 347 | -0.36 | 23% | -0.02 (113) | -0.69 (104) | -0.50 (69) | -0.17 (50) | -0.79 (11) | - |

| Strategy | `<= +2` | `+3` | `+4` | `>= +5` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | +0.06 (206) | +0.08 (359) | -0.02 (341) | -0.57 (117) |
| `MOMENTUM_BREAKOUT` | -0.37 (32) | -0.56 (47) | -0.45 (41) | -0.93 (10) |
| `EPISODIC_PIVOT` | +0.42 (261) | +0.15 (285) | +0.19 (153) | -0.24 (48) |
| `LEADER_PULLBACK` | -0.13 (509) | -0.17 (516) | -0.48 (340) | -0.15 (135) |
| `EMERGING_LEADER` | -0.17 (532) | -0.20 (79) | -0.29 (106) | -0.47 (37) |

- All: best `<= +2` vs worst `>= +5` +0.33 (+0.10 to +0.54); best ahead in 5 of 5 years -> **informative**.
- `VCP_BREAKOUT`: best `+3` vs worst `>= +5` +0.65 (+0.39 to +0.89); best ahead in 4 of 4 years -> **informative**.
- `MOMENTUM_BREAKOUT`: best `<= +2` vs worst `+3` +0.19 (-0.32 to +0.73); best ahead in 0 of 0 years -> not informative.
- `EPISODIC_PIVOT`: best `<= +2` vs worst `>= +5` +0.65 (-0.51 to +1.68); best ahead in 1 of 2 years -> not informative.
- `LEADER_PULLBACK`: best `<= +2` vs worst `+4` +0.35 (+0.15 to +0.55); best ahead in 5 of 5 years -> **informative**.
- `EMERGING_LEADER`: best `<= +2` vs worst `>= +5` +0.30 (+0.14 to +0.48); best ahead in 1 of 1 years -> not informative.

### D5: leadership pillar

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `<= 0` | 1930 | -0.09 | 29% | -0.06 (495) | -0.20 (361) | -0.05 (498) | +1.48 (96) | -0.43 (317) | -0.40 (163) |
| `> 0` | 2224 | -0.11 | 28% | +0.20 (565) | -0.67 (294) | -0.10 (354) | +0.05 (444) | -0.14 (358) | -0.43 (209) |

| Strategy | `<= 0` | `> 0` |
|---|---|---|
| `VCP_BREAKOUT` | +0.03 (466) | -0.09 (557) |
| `MOMENTUM_BREAKOUT` | -0.51 (55) | -0.50 (75) |
| `EPISODIC_PIVOT` | +0.34 (264) | +0.16 (483) |
| `LEADER_PULLBACK` | -0.20 (654) | -0.24 (846) |
| `EMERGING_LEADER` | -0.26 (491) | -0.11 (263) |

- All: best `<= 0` vs worst `> 0` +0.01 (-0.12 to +0.15); best ahead in 4 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `<= 0` vs worst `> 0` +0.12 (-0.09 to +0.32); best ahead in 4 of 5 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `> 0` vs worst `<= 0` +0.01 (-0.42 to +0.43); best ahead in 3 of 3 years -> not informative.
- `EPISODIC_PIVOT`: best `<= 0` vs worst `> 0` +0.18 (-0.37 to +0.97); best ahead in 4 of 6 years -> not informative.
- `LEADER_PULLBACK`: best `<= 0` vs worst `> 0` +0.04 (-0.13 to +0.21); best ahead in 4 of 6 years -> not informative.
- `EMERGING_LEADER`: best `> 0` vs worst `<= 0` +0.15 (+0.00 to +0.30); best ahead in 2 of 3 years -> not informative.

### D5: distribution pillar

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< 0` | 158 | -0.16 | 28% | +1.15 (5) | -0.24 (39) | -0.06 (10) | +0.23 (8) | -0.25 (96) | - |
| `0` | 3996 | -0.10 | 29% | +0.08 (1055) | -0.42 (616) | -0.07 (842) | +0.30 (532) | -0.28 (579) | -0.41 (372) |

| Strategy | `< 0` | `0` |
|---|---|---|
| `VCP_BREAKOUT` | +0.02 (28) | -0.03 (995) |
| `MOMENTUM_BREAKOUT` | -0.37 (5) | -0.51 (125) |
| `EPISODIC_PIVOT` | +0.05 (36) | +0.23 (711) |
| `LEADER_PULLBACK` | -0.30 (89) | -0.22 (1411) |
| `EMERGING_LEADER` | - | -0.21 (754) |

- All: best `0` vs worst `< 0` +0.07 (-0.16 to +0.29); best ahead in 0 of 3 years -> not informative.
- `EPISODIC_PIVOT`: best `0` vs worst `< 0` +0.18 (-0.50 to +0.81); best ahead in 1 of 1 years -> not informative.
- `LEADER_PULLBACK`: best `0` vs worst `< 0` +0.08 (-0.20 to +0.34); best ahead in 0 of 2 years -> not informative.

### D6: 50-session dollar volume

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< $25M` | 1140 | -0.15 | 27% | +0.03 (338) | -0.54 (203) | -0.28 (204) | +0.28 (151) | -0.18 (174) | -0.32 (70) |
| `$25-100M` | 1681 | -0.08 | 29% | -0.03 (433) | -0.41 (260) | +0.16 (352) | +0.46 (229) | -0.37 (288) | -0.52 (119) |
| `$100-500M` | 1139 | -0.13 | 30% | +0.23 (251) | -0.27 (174) | -0.26 (257) | -0.03 (130) | -0.18 (181) | -0.34 (146) |
| `>= $500M` | 194 | +0.09 | 30% | +0.87 (38) | -0.38 (18) | +0.18 (39) | +0.68 (30) | -0.52 (32) | -0.52 (37) |

| Strategy | `< $25M` | `$25-100M` | `$100-500M` | `>= $500M` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | -0.12 (261) | -0.00 (391) | -0.03 (317) | +0.14 (54) |
| `MOMENTUM_BREAKOUT` | -0.67 (43) | -0.39 (57) | -0.55 (25) | -0.13 (5) |
| `EPISODIC_PIVOT` | +0.08 (234) | +0.26 (340) | +0.17 (149) | +1.47 (24) |
| `LEADER_PULLBACK` | -0.19 (464) | -0.26 (601) | -0.22 (370) | -0.14 (65) |
| `EMERGING_LEADER` | -0.28 (138) | -0.13 (292) | -0.23 (278) | -0.34 (46) |

- All: best `>= $500M` vs worst `< $25M` +0.24 (-0.04 to +0.55); best ahead in 4 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `>= $500M` vs worst `< $25M` +0.26 (-0.26 to +0.87); best ahead in 2 of 3 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `$25-100M` vs worst `< $25M` +0.29 (-0.11 to +0.73); best ahead in 0 of 1 years -> not informative.
- `EPISODIC_PIVOT`: best `$25-100M` vs worst `< $25M` +0.19 (-0.34 to +0.81); best ahead in 3 of 6 years -> not informative.
- `LEADER_PULLBACK`: best `>= $500M` vs worst `$25-100M` +0.12 (-0.27 to +0.56); best ahead in 2 of 4 years -> not informative.
- `EMERGING_LEADER`: best `$25-100M` vs worst `>= $500M` +0.21 (+0.01 to +0.42); best ahead in 0 of 2 years -> not informative.

### D6: entry price

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< $10` | 18 | +3.54 | 44% | - | +0.49 (1) | +18.84 (1) | -1.04 (5) | +5.82 (8) | +0.99 (3) |
| `$10-20` | 587 | +0.23 | 30% | +0.15 (155) | -0.24 (64) | -0.01 (60) | +1.12 (124) | +0.01 (136) | -0.30 (48) |
| `$20-50` | 1539 | -0.18 | 29% | -0.05 (439) | -0.47 (217) | -0.11 (324) | +0.16 (189) | -0.37 (242) | -0.41 (128) |
| `>= $50` | 2010 | -0.17 | 28% | +0.18 (466) | -0.41 (373) | -0.10 (467) | -0.00 (222) | -0.50 (289) | -0.46 (193) |

| Strategy | `< $10` | `$10-20` | `$20-50` | `>= $50` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | - | -0.20 (94) | +0.02 (322) | -0.04 (607) |
| `MOMENTUM_BREAKOUT` | - | -0.66 (27) | -0.25 (59) | -0.76 (44) |
| `EPISODIC_PIVOT` | +3.54 (18) | +0.70 (226) | -0.15 (334) | -0.01 (169) |
| `LEADER_PULLBACK` | - | +0.06 (217) | -0.32 (573) | -0.23 (710) |
| `EMERGING_LEADER` | - | -0.06 (23) | -0.11 (251) | -0.26 (480) |

- All: best `$10-20` vs worst `$20-50` +0.40 (+0.12 to +0.74); best ahead in 6 of 6 years -> **informative**.
- `VCP_BREAKOUT`: best `$20-50` vs worst `$10-20` +0.22 (-0.13 to +0.55); best ahead in 3 of 4 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `$20-50` vs worst `>= $50` +0.51 (+0.02 to +0.99); best ahead in 1 of 1 years -> not informative.
- `EPISODIC_PIVOT`: best `$10-20` vs worst `$20-50` +0.85 (+0.21 to +1.73); best ahead in 5 of 6 years -> **informative**.
- `LEADER_PULLBACK`: best `$10-20` vs worst `$20-50` +0.38 (+0.14 to +0.64); best ahead in 5 of 6 years -> **informative**.
- `EMERGING_LEADER`: best `$20-50` vs worst `>= $50` +0.15 (+0.04 to +0.26); best ahead in 3 of 3 years -> not informative.

### D7: RS rating

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< 80` | 813 | -0.14 | 29% | +0.06 (253) | -0.34 (112) | -0.05 (129) | +0.01 (117) | -0.50 (154) | -0.19 (48) |
| `80-90` | 1535 | -0.15 | 29% | +0.07 (391) | -0.50 (265) | +0.01 (330) | +0.22 (158) | -0.37 (223) | -0.49 (168) |
| `>= 90` | 1805 | -0.04 | 28% | +0.10 (416) | -0.36 (278) | -0.15 (393) | +0.49 (264) | -0.09 (298) | -0.40 (156) |

| Strategy | `< 80` | `80-90` | `>= 90` |
|---|---|---|---|
| `VCP_BREAKOUT` | -0.11 (501) | +0.10 (319) | -0.07 (203) |
| `MOMENTUM_BREAKOUT` | -1.11 (1) | +1.31 (4) | -0.56 (125) |
| `EPISODIC_PIVOT` | -0.20 (311) | +0.19 (100) | +0.63 (335) |
| `LEADER_PULLBACK` | - | -0.29 (761) | -0.16 (739) |
| `EMERGING_LEADER` | - | -0.19 (351) | -0.22 (403) |

- All: best `>= 90` vs worst `80-90` +0.11 (-0.03 to +0.25); best ahead in 5 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `80-90` vs worst `< 80` +0.21 (-0.02 to +0.43); best ahead in 3 of 5 years -> not informative.
- `EPISODIC_PIVOT`: best `>= 90` vs worst `< 80` +0.83 (+0.30 to +1.50); best ahead in 5 of 6 years -> **informative**.
- `LEADER_PULLBACK`: best `>= 90` vs worst `80-90` +0.13 (-0.01 to +0.27); best ahead in 5 of 6 years -> not informative.
- `EMERGING_LEADER`: best `80-90` vs worst `>= 90` +0.03 (-0.08 to +0.14); best ahead in 2 of 3 years -> not informative.

### D7: group RS

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< 50` | 666 | -0.10 | 28% | +0.21 (134) | -0.37 (106) | +0.06 (146) | -0.06 (80) | -0.33 (132) | -0.29 (68) |
| `50-80` | 1046 | -0.06 | 31% | +0.44 (237) | -0.45 (150) | -0.11 (234) | +0.06 (129) | -0.26 (187) | -0.29 (109) |
| `>= 80` | 1333 | -0.11 | 31% | +0.07 (370) | -0.23 (202) | -0.09 (282) | +0.05 (166) | -0.17 (170) | -0.53 (143) |

| Strategy | `< 50` | `50-80` | `>= 80` |
|---|---|---|---|
| `VCP_BREAKOUT` | -0.02 (204) | +0.13 (284) | +0.01 (309) |
| `MOMENTUM_BREAKOUT` | -0.37 (22) | -0.75 (27) | -0.50 (34) |
| `EPISODIC_PIVOT` | -0.03 (184) | +0.06 (141) | +0.56 (138) |
| `LEADER_PULLBACK` | -0.27 (144) | -0.10 (365) | -0.24 (566) |
| `EMERGING_LEADER` | -0.11 (112) | -0.22 (229) | -0.24 (286) |

- All: best `50-80` vs worst `>= 80` +0.05 (-0.08 to +0.17); best ahead in 3 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `50-80` vs worst `< 50` +0.15 (-0.14 to +0.44); best ahead in 3 of 5 years -> not informative.
- `EPISODIC_PIVOT`: best `>= 80` vs worst `< 50` +0.59 (+0.03 to +1.15); best ahead in 2 of 4 years -> not informative.
- `LEADER_PULLBACK`: best `50-80` vs worst `< 50` +0.17 (-0.10 to +0.44); best ahead in 2 of 5 years -> not informative.
- `EMERGING_LEADER`: best `< 50` vs worst `>= 80` +0.12 (-0.09 to +0.34); best ahead in 1 of 3 years -> not informative.

### D7: extension above the 50-day

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< 5%` | 1209 | -0.14 | 29% | +0.09 (483) | -0.56 (165) | -0.10 (243) | -0.02 (121) | -0.49 (157) | -0.34 (40) |
| `5-15%` | 1952 | -0.20 | 29% | +0.11 (426) | -0.34 (379) | -0.13 (501) | -0.09 (169) | -0.52 (243) | -0.44 (234) |
| `15-25%` | 554 | -0.17 | 25% | -0.20 (103) | -0.62 (79) | +0.10 (71) | +0.34 (117) | -0.27 (119) | -0.60 (65) |
| `>= 25%` | 439 | +0.52 | 32% | +0.30 (48) | +0.02 (32) | +0.62 (37) | +1.07 (133) | +0.30 (156) | +0.04 (33) |

| Strategy | `< 5%` | `5-15%` | `15-25%` | `>= 25%` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | -0.05 (645) | +0.02 (370) | -0.47 (8) | - |
| `MOMENTUM_BREAKOUT` | - | -0.65 (33) | -0.68 (40) | -0.30 (57) |
| `EPISODIC_PIVOT` | -0.35 (83) | -0.14 (109) | -0.19 (173) | +0.64 (382) |
| `LEADER_PULLBACK` | -0.22 (438) | -0.28 (826) | -0.05 (236) | - |
| `EMERGING_LEADER` | -0.14 (43) | -0.22 (614) | -0.18 (97) | - |

- All: best `>= 25%` vs worst `5-15%` +0.72 (+0.34 to +1.23); best ahead in 6 of 6 years -> **informative**.
- `VCP_BREAKOUT`: best `5-15%` vs worst `< 5%` +0.07 (-0.11 to +0.26); best ahead in 4 of 5 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `>= 25%` vs worst `15-25%` +0.37 (-0.15 to +0.89); best ahead in 1 of 1 years -> not informative.
- `EPISODIC_PIVOT`: best `>= 25%` vs worst `< 5%` +1.00 (+0.41 to +1.65); best ahead in 5 of 5 years -> **informative**.
- `LEADER_PULLBACK`: best `15-25%` vs worst `5-15%` +0.22 (-0.04 to +0.50); best ahead in 2 of 5 years -> not informative.
- `EMERGING_LEADER`: best `< 5%` vs worst `5-15%` +0.07 (-0.15 to +0.30); best ahead in 1 of 1 years -> not informative.

### D7: ADR

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `< 3%` | 2454 | -0.18 | 29% | +0.08 (754) | -0.40 (444) | -0.08 (655) | -0.16 (148) | -0.69 (227) | -0.45 (226) |
| `3-5%` | 997 | -0.18 | 28% | +0.08 (234) | -0.46 (160) | -0.20 (158) | -0.02 (175) | -0.30 (195) | -0.38 (75) |
| `5-8%` | 427 | +0.36 | 31% | +0.02 (53) | -0.22 (38) | -0.02 (31) | +1.20 (136) | +0.12 (130) | -0.48 (39) |
| `>= 8%` | 276 | +0.20 | 27% | +0.27 (19) | -0.62 (13) | +2.95 (8) | +0.34 (81) | +0.10 (123) | -0.17 (32) |

| Strategy | `< 3%` | `3-5%` | `5-8%` | `>= 8%` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | +0.01 (822) | -0.24 (187) | +0.27 (14) | - |
| `MOMENTUM_BREAKOUT` | -0.17 (13) | -0.84 (38) | -0.56 (48) | -0.15 (31) |
| `EPISODIC_PIVOT` | -0.28 (128) | -0.06 (233) | +0.88 (183) | +0.28 (203) |
| `LEADER_PULLBACK` | -0.35 (840) | -0.13 (437) | +0.05 (181) | +0.05 (42) |
| `EMERGING_LEADER` | -0.20 (651) | -0.30 (102) | +4.35 (1) | - |

- All: best `5-8%` vs worst `< 3%` +0.54 (+0.17 to +1.01); best ahead in 4 of 6 years -> **informative**.
- `VCP_BREAKOUT`: best `< 3%` vs worst `3-5%` +0.25 (+0.03 to +0.45); best ahead in 4 of 5 years -> **informative**.
- `MOMENTUM_BREAKOUT`: best `>= 8%` vs worst `3-5%` +0.70 (+0.14 to +1.33); best ahead in 0 of 0 years -> not informative.
- `EPISODIC_PIVOT`: best `5-8%` vs worst `< 3%` +1.17 (+0.33 to +2.27); best ahead in 4 of 5 years -> **informative**.
- `LEADER_PULLBACK`: best `>= 8%` vs worst `< 3%` +0.40 (-0.06 to +0.92); best ahead in 2 of 2 years -> not informative.
- `EMERGING_LEADER`: best `< 3%` vs worst `3-5%` +0.10 (-0.08 to +0.25); best ahead in 2 of 3 years -> not informative.

### D7: EPS growth

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `none or <= 0` | 2870 | -0.10 | 28% | -0.06 (691) | -0.46 (446) | -0.08 (552) | +0.41 (442) | -0.20 (524) | -0.37 (215) |
| `0-25%` | 365 | -0.10 | 31% | +0.31 (103) | -0.24 (49) | -0.15 (123) | -0.33 (33) | -0.70 (24) | -0.36 (33) |
| `> 25%` | 919 | -0.10 | 31% | +0.35 (266) | -0.34 (160) | +0.01 (177) | -0.11 (65) | -0.50 (127) | -0.50 (124) |

| Strategy | `none or <= 0` | `0-25%` | `> 25%` |
|---|---|---|---|
| `VCP_BREAKOUT` | -0.15 (626) | +0.23 (121) | +0.11 (276) |
| `MOMENTUM_BREAKOUT` | -0.46 (121) | -1.19 (1) | -1.04 (8) |
| `EPISODIC_PIVOT` | +0.23 (672) | -0.24 (28) | +0.39 (47) |
| `LEADER_PULLBACK` | -0.21 (1020) | -0.24 (111) | -0.24 (369) |
| `EMERGING_LEADER` | -0.19 (431) | -0.30 (104) | -0.20 (219) |

- All: best `> 25%` vs worst `0-25%` +0.00 (-0.17 to +0.17); best ahead in 4 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `0-25%` vs worst `none or <= 0` +0.38 (+0.02 to +0.77); best ahead in 4 of 4 years -> **informative**.
- `EPISODIC_PIVOT`: best `> 25%` vs worst `none or <= 0` +0.16 (-0.46 to +0.86); best ahead in 2 of 2 years -> not informative.
- `LEADER_PULLBACK`: best `none or <= 0` vs worst `0-25%` +0.03 (-0.22 to +0.26); best ahead in 2 of 4 years -> not informative.
- `EMERGING_LEADER`: best `none or <= 0` vs worst `0-25%` +0.11 (-0.04 to +0.26); best ahead in 2 of 3 years -> not informative.

### D7: base stage

| Bucket | Trades | Expectancy (R) | Win rate | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|---|---|---|
| `1` | 1582 | -0.12 | 29% | -0.04 (309) | -0.43 (207) | +0.02 (432) | +0.19 (239) | -0.37 (189) | -0.37 (206) |
| `2` | 1989 | -0.14 | 29% | +0.15 (568) | -0.37 (335) | -0.22 (367) | -0.09 (243) | -0.21 (333) | -0.41 (143) |
| `3+` | 583 | +0.07 | 28% | +0.08 (183) | -0.50 (113) | +0.19 (53) | +2.42 (58) | -0.31 (153) | -0.82 (23) |

| Strategy | `1` | `2` | `3+` |
|---|---|---|---|
| `VCP_BREAKOUT` | -0.17 (343) | +0.03 (504) | +0.06 (176) |
| `MOMENTUM_BREAKOUT` | -0.28 (37) | -0.62 (68) | -0.53 (25) |
| `EPISODIC_PIVOT` | +0.02 (383) | +0.18 (282) | +1.34 (82) |
| `LEADER_PULLBACK` | -0.20 (483) | -0.25 (779) | -0.19 (238) |
| `EMERGING_LEADER` | -0.11 (336) | -0.28 (356) | -0.32 (62) |

- All: best `3+` vs worst `2` +0.21 (-0.06 to +0.54); best ahead in 2 of 6 years -> not informative.
- `VCP_BREAKOUT`: best `3+` vs worst `1` +0.22 (-0.11 to +0.58); best ahead in 4 of 5 years -> not informative.
- `MOMENTUM_BREAKOUT`: best `1` vs worst `2` +0.34 (-0.24 to +0.98); best ahead in 0 of 1 years -> not informative.
- `EPISODIC_PIVOT`: best `3+` vs worst `1` +1.32 (-0.24 to +3.50); best ahead in 3 of 4 years -> not informative.
- `LEADER_PULLBACK`: best `3+` vs worst `2` +0.06 (-0.16 to +0.30); best ahead in 3 of 5 years -> not informative.
- `EMERGING_LEADER`: best `1` vs worst `3+` +0.21 (-0.03 to +0.43); best ahead in 1 of 2 years -> not informative.

## D8: anatomy of the losses

### The exit rule

| Exit | Trades | Share | Mean R |
|---|---|---|---|
| `HARD_STOP` | 2467 | 59% | -0.94 |
| `TRAIL` | 1267 | 31% | +1.42 |
| `EARNINGS_CUSHION` | 156 | 4% | +0.21 |
| `CLIMAX` | 141 | 3% | +0.81 |
| `HEAVY_VOLUME_REVERSAL` | 123 | 3% | -0.42 |

### The exit session

| Session | Trades | Mean R |
|---|---|---|
| 1 | 469 | -1.04 |
| 2-5 | 1436 | -0.93 |
| 6-20 | 1579 | -0.04 |
| > 20 | 670 | +2.19 |

### Failed breakouts and given-back gains

| Strategy | Trades | Stopped out at a loss within 5 sessions | Losers that closed at +1R first | Mean of the best close (R) |
|---|---|---|---|---|
| **All** | 4154 | 39% | 12% of 2964 | +1.13 |
| `VCP_BREAKOUT` | 1023 | 25% | 13% of 689 | +1.30 |
| `MOMENTUM_BREAKOUT` | 130 | 68% | 17% of 105 | +0.76 |
| `EPISODIC_PIVOT` | 747 | 64% | 17% of 536 | +1.59 |
| `LEADER_PULLBACK` | 1500 | 44% | 14% of 1109 | +1.17 |
| `EMERGING_LEADER` | 754 | 15% | 2% of 525 | +0.40 |

## D9: the charts

Drawn at random (seed 38) and read by eye; the files are in `var/backtests/diagnostics/`. Red: the pivot; grey: the initial stop.

| File | Strategy | Scan session | Entry | R | Exit |
|---|---|---|---|---|---|
| vcp_breakout-AKAM-2019-07-30.png | `VCP_BREAKOUT` | 2019-07-30 | 2019-07-31 | -0.70 | `TRAIL` |
| vcp_breakout-WCG-2018-01-11.png | `VCP_BREAKOUT` | 2018-01-11 | 2018-01-12 | -0.74 | `TRAIL` |
| vcp_breakout-KMPR-2018-01-16.png | `VCP_BREAKOUT` | 2018-01-16 | 2018-01-17 | -1.07 | `HARD_STOP` |
| momentum_breakout-BLUE-2017-10-16.png | `MOMENTUM_BREAKOUT` | 2017-10-16 | 2017-10-17 | -1.07 | `HARD_STOP` |
| momentum_breakout-KITE-2017-07-19.png | `MOMENTUM_BREAKOUT` | 2017-07-19 | 2017-07-20 | -1.08 | `HARD_STOP` |
| momentum_breakout-NFE-2020-12-14.png | `MOMENTUM_BREAKOUT` | 2020-12-14 | 2020-12-15 | -1.54 | `HARD_STOP` |
| leader_pullback-AMN-2021-12-07.png | `LEADER_PULLBACK` | 2021-12-07 | 2021-12-08 | -0.97 | `TRAIL` |
| leader_pullback-CRSP-2019-12-19.png | `LEADER_PULLBACK` | 2019-12-19 | 2019-12-20 | -1.06 | `HARD_STOP` |
| leader_pullback-VEEV-2019-03-25.png | `LEADER_PULLBACK` | 2019-03-25 | 2019-03-26 | -1.03 | `HARD_STOP` |
| emerging_leader-LULU-2018-04-16.png | `EMERGING_LEADER` | 2018-04-16 | 2018-04-17 | +0.11 | `TRAIL` |
| emerging_leader-EC-2022-04-04.png | `EMERGING_LEADER` | 2022-04-04 | 2022-04-05 | -1.22 | `HARD_STOP` |
| emerging_leader-PBA-2022-06-07.png | `EMERGING_LEADER` | 2022-06-07 | 2022-06-08 | -1.04 | `HARD_STOP` |

## Reading

*Written 2026-10-24 by hand, read by the rules of the pre-registration.*

> **Update (same day):** the EP fix in point 2 was approved and built (#143); the fixed EP results are in [baseline.md](baseline.md). **Every EP number on this page predates the fix and looks ahead.** The strategy decision this report leads to is in [docs/strategy-decision.md](../strategy-decision.md).

**1. Costs are the largest single factor, but not the whole story (rule 1).** Slippage costs the pool +0.13R a trade (+0.12 to +0.14). Without it the pool is +0.03R (-0.04 to +0.11): still not a pass, so costs are not "the main cause" by rule 1, and far above the +0.05R that would rule them out. Costs weigh most where stops are tight: a 0.2% slippage each way is 0.13R on a 3% stop and 0.4R on a 1% stop. Stops under 2% wide are common: a quarter of momentum, pullback and EP trades have them, because the 09:36 low of the day sits close to the trigger. Those trades average -0.5 to -0.9R. D3 confirms it for pullbacks: < 3% stops are 0.50R worse than 5-7%, in 6 of 6 years.

**2. A bug: the EP proxy looks ahead (rule 4).** `EpisodicPivot.scan` qualifies a gap on the gap session's **full-day** volume, and the pool enters that same session at 09:36, before that volume exists. The ranking features (extension, RS rating, ADR, the RS line) are also computed on the gap session's close. So an EP that held its gap all day is more likely to be listed, ranked high and entered. That is exactly the shape of the EP splits: the top quarter is +1.19R, more than 25% above the 50-day is +0.64R, and RS >= 90 is +0.63R. The EP results (+0.23R, the only positive strategy) and every EP split above are therefore not evidence until the proxy uses only what is known at 09:36. This changes the proxy that README §5.3 describes, so the fix goes to the user.

**3. The other four strategies lose, and the setups look right (D9).** Without EPs the pool is **-0.17R** (90% -0.23 to -0.12) over 3,407 trades. In the 9 non-EP charts the code found what the README describes: stage-2 stocks with aligned averages, pivots at the top of a base or above a pullback to the 10/21-day, and stops below it. Nothing points to an implementation error in these four scans. D8 shows the losses are early failures: 39% of all trades are stopped out at a loss within 5 sessions, 469 on the entry day itself. Only 12% of losers had closed at +1R first, so the exits aren't giving back gains (as 3.8's comparisons found).

**4. What the splits say about the non-EP strategies:**
- **The regime works backwards for breakouts:** the higher the score the evening before, the worse the trade (all: <= +2 is -0.03R, >= +5 is -0.36R; informative for VCPs and pullbacks, 4/4 and 5/5 years). Buying breakouts when the market is most extended loses. No source supports filtering on that, so it is reported, not proposed.
- **Ranking helps a little:** rank 1-10 beats 11-20 by +0.26R (6 of 6 years), but much of that is EPs. Non-EP rank 1-10 is still -0.14R (-0.23 to -0.06).
- **VCP with growing earnings:** EPS growth > 0 is +0.38R better than none (4 of 4 years), and O'Neil's C and A and Minervini's SEPA both require it. On its own it gives +0.15R (90% -0.04 to +0.34) over 397 trades: it doesn't pass the gate.
- Not informative: the leadership and distribution pillars, dollar volume, group RS, base stage.

**5. Rule 3: no candidate fix passes on its own.** The one source-backed candidate (VCP with EPS growth > 0) misses the gate. Non-EP filters by rank or EPS growth stay negative (-0.14R and -0.12R).

**What this says:** for VCPs, momentum breakouts, leader pullbacks and emerging leaders, the loss is **built in**: the setups as specified, entered at 09:36 with low-of-day or ATR stops, don't pay in this universe after costs. Costs and tight stops are a large part of why. EPs are **undecided**: the only positive result rests on a look-ahead, so the proxy has to be fixed and EPs re-run before anything is concluded. The holdout is still closed.
