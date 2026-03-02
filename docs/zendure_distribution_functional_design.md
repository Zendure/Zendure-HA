# Zendure HA — Complete Functional Design: `distribution.py`

---

## Table of Contents

1. [Purpose](#1-purpose)
2. [Core Concepts](#2-core-concepts)
3. [Operation Modes](#3-operation-modes)
4. [Device Count Strategy](#4-device-count-strategy)
5. [Setpoint Calculation](#5-setpoint-calculation)
6. [Signal Processing](#6-signal-processing)
7. [Deadband](#7-deadband)
8. [Hysteresis](#8-hysteresis)
9. [Direction Change Guard](#9-direction-change-guard)
10. [Power Distribution Algorithm](#10-power-distribution-algorithm)
11. [Strategy Threshold Interaction with Deadband and Guard](#11-strategy-threshold-interaction-with-deadband-and-guard)
12. [Constants and Parameters Reference](#12-constants-and-parameters-reference)
13. [Known Limitations](#13-known-limitations)

---

## 1. Purpose

The `Distribution` class manages the power output and input of multiple Zendure battery devices in order to keep the net power at the grid connection point (P1 meter) as close to zero as possible — i.e. **zero export and zero import**. It does this by continuously reacting to P1 meter readings every 2 seconds and redistributing power across all available devices, while respecting hardware limits, fuse group constraints, battery state of charge, the configured operation mode, the configured device count strategy, and the configured signal processing settings.

---

## 2. Core Concepts

### P1 Meter
The P1 meter measures the net power at the grid connection point:
- **Positive value**: the home is consuming power from the grid (deficit).
- **Negative value**: the home is exporting power to the grid (surplus).

The goal is to drive this value towards zero by instructing devices to discharge (cover a deficit) or charge (absorb a surplus).

### Setpoint
The setpoint is the total power that all devices combined must deliver or absorb to reach zero on the meter. It is derived from the P1 reading, corrected for the current actual output of all active devices and any off-grid loads, then passed through the signal processing pipeline. The resulting setpoint is published to a Home Assistant sensor entity for observability.

### Devices
Each device is a Zendure battery unit that can both charge (absorb power from the grid or solar) and discharge (deliver power to home loads). Key properties per device:
- **`solarPower`**: power from a solar panel connected directly to the device.
- **`homePower`**: current actual power exchange with the home circuit — negative means the device is being charged (consuming), positive means it is discharging (delivering).
- **`availableKwh`**: currently stored energy, corrected for user-configured minimum and maximum state of charge.
- **`kWh`**: total usable capacity (also corrected for SoC limits).
- **`level`**: current state of charge as a percentage.
- **`limit[0]`** / **`limit[1]`**: maximum charge power / maximum discharge power.
- **`offGrid`**: if the device has a 220V off-grid socket in use, this represents the power flowing through it. A negative value means the socket is delivering power (e.g. bypassing country export limits such as the German 800W rule); a positive value means it is consuming power from the device independently.
- **`fuseGrp`**: the fuse group the device belongs to (see below).

#### Device capabilities
| Device name | AC charge | Solar input | offGrid socket | AC ouput |
|---|---|---|---|---|
| AIO 2400 | no | yes | no | yes |
| HUB 1200 | no | yes | no | yes [^1] |
| HUB 2000 | no | yes | no | yes [^1] |
| ACE 1500 | yes | yes | yes | no [^2] |
| HYPER 2000 | yes | yes | no [^3] | yes |
| SF 800 | yes | yes | no | yes |
| SF 800 plus| yes | yes | no | yes |
| SF 800 pro | yes | yes | yes | yes |
| SF 1600 AC+| yes | no | yes | yes |
| SF 2400 AC(+) | yes | no | yes | yes |
| SF 2400 pro | yes | yes | yes | yes |

[^1]: via Micro-Inverter
[^2]: only via Hub and Micro-Inverter
[^3]: it exists a satelite plug, but then the devices is standalone
### Fuse Groups
Multiple devices may share the same electrical circuit, which has a maximum total power capacity (the fuse limit). The `fuseGrp` object tracks the combined power of all devices in the group and ensures no single device is assigned more power than the group's remaining headroom allows. **The fuse group limit is a legal hard cap and must never be exceeded under any circumstances**, including during device startup windows. It not necessarily means, that devices in one fusegroup need to be connected to one fuse or same electrical circuit. The fusegroups can be used to fullfil the 800W limitation if using several devices. 

### Response Times
- A device takes approximately **4 seconds** to respond to a power command.
- A newly started device takes approximately **15 seconds** before it begins delivering power.
- The distribution is recalculated every **2 seconds**.
- The minimum efficient power for a device is **55W**. Below this, the device itself may refuse to start or operates inefficiently.

---

## 3. Operation Modes

| Mode | Behaviour |
|---|---|
| **OFF** | All devices are powered off. No distribution takes place. |
| **MATCHING** | Devices freely charge and discharge to maintain zero on the meter. |
| **MATCHING_DISCHARGE** | Devices only discharge. The setpoint is clamped to a minimum of zero. Devices cover home consumption but never charge from the grid. Used when electricity prices are high and maximum self-consumption is desired. |
| **MATCHING_CHARGE** | Devices only charge. The setpoint is clamped to a maximum of zero. Devices absorb surplus power but never discharge. Used when electricity prices are low. |
| **MANUAL** | A fixed power setpoint is used, as specified by the user via a Home Assistant entity. Typically used for maximum-speed charging during cheap electricity periods, independent of the P1 meter reading. In MANUAL mode the deadband does not apply. |
> [!NOTE]
> Need to define the behavior of the PV and offGrid inputs for MATCHING DISCHARGE and MATCHING CHARGE

When switching to OFF mode, all devices are instructed to stop. If no devices are online when attempting to start any other mode, a persistent Home Assistant notification is raised and the mode change is rejected. Mode and strategy changes take effect immediately on the next 2-second update cycle. Any attached PV will load the batteries.
> [!NOTE]
> What happens in bypass if export of power is allowed and the batteries are full? 

---

## 4. Device Count Strategy

The strategy controls how many devices are kept active simultaneously. It is a configurable setting with three options:

| Strategy | Description | Stop threshold (per device) |
|---|---|---|
| **As many as possible** | Keep all devices active as long as each is above the absolute minimum. Maximises responsiveness to sudden demand spikes. | 20% of device limit |
| **As few as possible** | Concentrate power on the fewest devices that can cover the setpoint, keeping others in reserve. | 80% of device limit |
| **Optimal** | Use enough devices so that each operates in the inverter's optimal efficiency range. | 25% of device limit |

> [!NOTE]
> My experiance is, that 25% is too low, a good choice in my opinion is device limit / 2.5 == 40%. Especially the SF 2400 does not work efficiently with too low power.

The start threshold (when to add a new device) is **80% average load across active devices**, the same for all strategies. The difference between strategies is entirely in the stop threshold — how low a device is allowed to run before the stop timer begins counting down. Strategy changes take effect immediately on the next update cycle.

The strategy thresholds are expressed as a percentage of each device's individual power limit, so devices with different maximum capacities have different absolute watt values but follow the same percentage-based rules.

---

## 5. Setpoint Calculation

### Step 1 — Raw setpoint from P1
The raw setpoint starts as the current P1 meter reading (in watts, converted from kW if necessary).

### Step 2 — Correction for active device output
For each active device, its current `homePower` is added back to the setpoint. This removes the effect of what devices are already doing, giving the true underlying home consumption or surplus. Solar power connected to devices is tracked separately. Off-grid socket power is also accounted for: if a device is delivering via its off-grid socket, this is added to the solar total; if the off-grid socket is consuming independently, it is subtracted from the setpoint.

### Step 3 — Signal processing pipeline
The corrected setpoint is passed through the full signal processing pipeline described in sections 6–9. In summary: EMA smoothing → rate limiting → standard deviation calculation → hysteresis peak filtering → sustained duration check → deadband → direction change guard.

The old 5-sample SMA, the `CONST_POWER_JUMP` history-clear logic, and the sign-change reset (force to zero when sign differs from average) are all removed, as the EMA and direction change guard handle these cases more precisely.

### Step 4 — Mode clamping
The processed setpoint is clamped according to the active operation mode:
- `MATCHING_DISCHARGE`: clamp to ≥ 0 (no charging).
- `MATCHING_CHARGE`: clamp to ≤ 0 (no discharging).
- `MANUAL`: use the fixed manual setpoint directly, bypassing the pipeline.
- `MATCHING` / `OFF`: no clamping.

---

## 6. Signal Processing

### Purpose
Before any distribution decision is made, the raw corrected setpoint must be processed into a stable, meaningful signal. The pipeline handles both fast transient spikes and genuine sustained load changes, and provides the volatility measure (σ) used by the deadband and hysteresis layers.

---

### Layer 1 — Exponential Moving Average (EMA)

The EMA gives exponentially more weight to recent readings, making it faster to track real changes while still smoothing noise. It replaces the old 5-sample simple moving average.

```
EMA = α × current_setpoint + (1 − α) × previous_EMA
```

The α parameter (0.0–1.0) controls the response speed:
- High α (e.g. 0.5): reacts quickly, less smoothing.
- Low α (e.g. 0.1): reacts slowly, more smoothing.

**Default: α = 0.3** (balanced for the ~4 second device response time).

For non-expert users, α is presented as a named setting:

| Setting | α value |
|---|---|
| Slow | 0.1 |
| Medium (default) | 0.3 |
| Fast | 0.5 |

Expert users configure α directly as a decimal value.

---

### Layer 2 — Rate Limiter

If the change between the previous EMA value and the current EMA value exceeds the configured threshold, the change is damped to the configured factor of the delta, towards the EMA value:

```
if abs(current_EMA − previous_EMA) > rate_limit_threshold:
    setpoint = previous_EMA + rate_limit_factor × (current_EMA − previous_EMA)
```

This replaces the hardcoded `CONST_POWER_JUMP_HIGH` logic and damps towards the EMA rather than the old rolling average.

| Parameter | Default | Description |
|---|---|---|
| `rate_limit_threshold` | 250W | Change above which damping is applied |
| `rate_limit_factor` | 0.75 | Fraction of the delta to apply (expert configurable) |

---

### Layer 3 — Standard Deviation

The standard deviation σ of the EMA-smoothed setpoint over the last 60 seconds (30 samples at 2-second intervals) is calculated continuously and used by the deadband and hysteresis layers.

```
σ = standard_deviation(EMA_history, last 30 samples)
```

A high σ indicates a volatile period (cloudy weather, cyclic appliances). A low σ indicates a calm, stable period. σ is not used directly as a setpoint filter — it informs the adaptive width of the deadband and the peak detection threshold of the hysteresis layer.

---

### Pipeline Summary

```
Raw P1 reading
    ↓
Correction for device output and off-grid sockets    (Section 5, Steps 1–2)
    ↓
EMA smoothing              (α configurable, default 0.3)
    ↓
Rate limiter               (threshold 250W, factor 75%, both configurable)
    ↓
σ calculation              (over last 60 seconds, 30 samples)
    ↓
Hysteresis peak filter     (Section 8 — 2×σ threshold)
    ↓
Sustained duration check   (Section 8 — Precise / Moderate / Relaxed)
    ↓
Deadband                   (Section 7 — shift + adaptive width)
    ↓
Mode clamping              (Section 5, Step 4)
    ↓
Direction change guard     (Section 9 — freeze on large drop, max 8s)
    ↓
Distribution algorithm     (Section 10)
```

---

## 7. Deadband

### Purpose
The deadband defines a window around the zero target within which the system does not aggressively react. It prevents unnecessary charge/discharge switching caused by small fluctuations, allows seasonal biasing (winter/summer), and automatically widens during volatile periods.

### Background
Victron ESS uses a fixed grid setpoint with no adaptive deadband. Community experience shows this causes control loop oscillation when large loads switch — the system overshoots and hunts. The Zendure design avoids this by making the band adaptive and mode-aware by default.

---

### Structure

**1. Mode-dependent shift (centre offset)**
The centre of the band is shifted from zero by a fixed number of watts per mode:

- **Negative shift** (e.g. −50W): targets slight net import, reacts earlier to export. **Winter / maximal solar** behaviour.
- **Positive shift** (e.g. +50W): tolerates slight net export, prioritises battery availability. **Summer / minimal grid import** behaviour.
- **Zero shift**: normal mode.
- **Minimal switching**: zero shift but large fixed minimum band width.

**2. Adaptive width (standard deviation based)**
```
band_width = fixed_minimum + stddev_multiplier × σ
```

The band is always symmetric around the shifted centre. On calm days it stays narrow; on volatile days it widens automatically.

---

### Mode Defaults

| Mode | Shift | Fixed minimum band | Behaviour |
|---|---|---|---|
| **Normal** | 0W | ±20W | Narrow on calm days, widens when volatile |
| **Maximal solar (winter)** | −50W (configurable) | ±20W | Reacts early to export; tolerates import |
| **Minimal from grid (summer)** | +50W (configurable) | ±20W | Reacts early to import; tolerates export |
| **Minimal switching** | 0W | ±150W (configurable) | Wide floor prevents switching even on calm days |

---

### Behaviour Inside the Deadband
- The system **gently drifts** device power output towards zero at a configurable rate (default: 15W per update cycle, ~450W per minute).
- It does not hold completely still — gradual release avoids the sharp transitions that cause oscillation.
- Device stop/start decisions continue via the stop timer mechanism and are not gated by the deadband edges.

### Behaviour Outside the Deadband
The normal distribution algorithm applies fully. The setpoint is passed to device distribution as processed by the pipeline.

### Interaction with Operation Modes
The deadband applies on top of mode clamping. In `MANUAL` mode the deadband is bypassed entirely.

---

## 8. Hysteresis

### Purpose
Hysteresis addresses the fundamental mismatch between load switching speed (milliseconds) and device response time (~4 seconds response, ~15 seconds to start). The goal is to respond to real sustained changes while ignoring transients that will resolve before any device can react.

### Background
Victron ESS has no built-in load transient filtering, causing overshoot and oscillation after large load changes. SolarEdge uses a fixed minimum response time delay, which is simple but crude — it delays all responses equally regardless of whether the change is a microwave cycling for 10 seconds or an EV charger that started permanently. The Zendure design improves on both by combining statistical peak detection with a user-configurable sustained duration threshold.

---

### Types of Load Event

**Type 1 — Sustained new load** (e.g. oven, EV charger)
A step change in P1 that persists. The system should respond fully and promptly.

**Type 2 — Cyclic load** (e.g. microwave cycling 100W↔900W every few seconds, dishwasher heating element)
Rapid oscillation around a mean. The system should track the **mean**, not the peaks.

**Type 3 — Brief transient** (e.g. coffee machine for 3 minutes, tumble dryer start surge)
A temporary excursion that resolves on its own. Whether to react depends on user preference.

---

### Mechanism 1 — Statistical Peak Filtering

At each update cycle, the current EMA setpoint is compared to the 60-second mean and σ:

- If the current setpoint deviates from the 60-second mean by **more than 2×σ**, it is a **peak event** — a statistical outlier. The setpoint used for distribution is a weighted blend: predominantly the 60-second mean, with a small contribution from the current value.
- If within **2×σ of the mean**, it is treated as a genuine sustained change and the system moves gently towards the current value.

This handles the microwave example correctly: the 100W↔900W cycling produces a mean of ~500W with high σ. Each individual reading exceeds 2σ in alternating directions, so the system tracks ~500W rather than chasing each cycle. Once the microwave stops, mean and σ settle, and the system correctly responds to the new lower load.

The peak detection threshold is configurable by expert users (default 2×σ, range 1.0–3.0×σ).

---

### Mechanism 2 — Sustained Duration Threshold

A configurable minimum duration controls how long a setpoint must remain outside the deadband before the system commits to a full response:

| Profile | Duration | Description |
|---|---|---|
| **Precise** | 0s | Respond immediately to any change outside the deadband. Best for stable loads. |
| **Moderate** | 30s | Ignore changes resolving within 30 seconds. Filters coffee machines, short surges, brief cloud cover. |
| **Relaxed** | 120s | Only respond to changes sustained for 2+ minutes. Best for users with many cyclic appliances. |

For non-expert users these are named profiles. Expert users configure the duration directly in seconds. During the window, the system still applies gentle drift — it does not hold completely still.

---

### Device Start/Stop Hysteresis

The signal hysteresis above operates upstream of device decisions. Device start/stop decisions use the separate linear stop timer mechanism (Section 10). These two mechanisms are independent and do not need to be tuned together.

---

## 9. Direction Change Guard

### Purpose
When a large load switches off suddenly, discharging devices continue delivering their commanded power for ~4–5 seconds during response lag. The P1 meter immediately shows net export, which would normally trigger the algorithm to start charging — creating a charge/discharge conflict where some devices are still discharging while others begin to charge. The guard prevents this by freezing device commands during the lag window.

---

### Trigger Condition

```
if (previous_setpoint − current_setpoint) > guard_threshold
   and current_commanded_power > 0:
       activate guard
```

The guard threshold defaults to 250W but is a **separate configurable parameter** from the rate limiter threshold, allowing independent tuning.

---

### Guard Behaviour

Once activated:
- All device power commands are **frozen** at their current values.
- The guard holds until **all active devices report `homePower` within ±15W of their commanded setpoint** (hardware accuracy limit), confirming they have caught up.
- A **maximum of 8 seconds** applies. If devices have not confirmed within 8 seconds, the guard releases and a **warning is logged**. No persistent HA notification is raised — this is a diagnostic warning only.
- While the guard is active, **stop timers for all devices are also frozen** — a device below its strategy threshold does not accumulate additional stop time during the freeze.

---

### Exception: Override in the Opposite Direction

During the guard window, if consumption **increases by more than the guard threshold** above the frozen setpoint (a new large load has switched on), the guard **immediately releases** and the system returns to full normal operation. The rate limiter provides sufficient overshoot protection on recovery. No additional ramp-up delay is applied.

---

### Relationship to Other Mechanisms

The guard is the **final layer** before commands reach devices, operating on **commanded power** rather than the setpoint signal. The rate limiter (Section 6) reduces the chance of triggering the guard; the guard handles the cases that slip through.

---

## 10. Power Distribution Algorithm

Once the final setpoint exits the pipeline, power is distributed across devices. Two paths exist:

### Solar-Only Mode
If solar power across all devices exceeds the setpoint and the setpoint is positive, no battery energy is used — solar production alone is sufficient to cover demand. Devices are sorted by ascending SoC and each is assigned at most its own solar production. This prevents the inefficiency of one device charging while another is discharging.

### Normal Distribution (Charge or Discharge)
The direction is determined by the sign of the setpoint: negative = charge (idx=0), positive = discharge (idx=1).

#### Weighting
- **Charging**: weight = remaining capacity to fill (`kWh − availableKwh`). Fully charged devices (level = 100%) get zero weight.
- **Discharging**: weight = available stored energy (`availableKwh`). Empty devices (level = 0%) get zero weight.

This ensures all devices charge or discharge proportionally to their capacity, reaching full or empty at approximately the same time.

#### Device Selection and Sorting
- **Charging**: sorted by lowest SoC first.
- **Discharging**: sorted by highest SoC first.

Devices already active (`homePower ≠ 0`) receive a slight sort priority boost to avoid unnecessary cycling. Devices without a fuse group or not in `ACTIVE` state are skipped.

#### Starting Devices
A device with zero weight is immediately stopped. Otherwise:
- If average load across active devices exceeds **80%** of their limits, additional devices are started.
- If the setpoint requires more capacity than active devices can cover, as many devices as necessary are started simultaneously.
- Each new device receives a start pulse (±50W). During its ~15-second startup window, already-active devices deliver at **100% capacity** (subject to fuse group hard limits) to cover the full setpoint.

#### Stopping Devices
When the setpoint drops and fewer devices are needed, the device with the **least `availableKwh`** is stopped first. Stopping follows a **linear stop delay**:

- At the strategy threshold: **60-second** delay.
- At the absolute minimum (55W): **2-second** delay.
- Between these points: linearly interpolated.

While devices are below the threshold but above 55W, power is distributed proportionally across all active devices by weight and individual limits — all devices drop together rather than one being run down alone. This means in a multi-device setup it is unlikely any single device reaches 55W quickly.

When a device is stopped, **all remaining devices' stop counters reset immediately**, and power is redistributed on the next 2-second update cycle.

#### Power Allocation
For each active device:
1. **Fixed base**: a share proportional to the device's capacity limit, up to 10% of total power. Ensures all active devices receive at least a minimal allocation.
2. **Flexible**: the remainder distributed proportionally to each device's weight.
3. Result is clamped to the fuse-group-adjusted limit (hard cap, never exceeded) and the remaining unallocated setpoint.

Allocation is sequential — after each device executes, the remaining setpoint and totals update, so the last device absorbs any remainder and prevents rounding errors from accumulating.

---

## 11. Strategy Threshold Interaction with Deadband and Guard

**Stop decisions and the deadband:**
The stop delay timer runs independently of the deadband. A device may be below its strategy threshold while the setpoint is inside the deadband — the gentle drift reduces commanded power and the stop timer counts down simultaneously. This is intentional: the deadband controls signal aggressiveness, the stop timer controls device efficiency.

**Start decisions and the deadband:**
A new device is only started if the setpoint is **outside** the deadband and average load exceeds 80%. Starting a device inside the deadband would overshoot in the opposite direction.

**Strategy thresholds and the direction change guard:**
The guard freezes all device commands and all stop timers. A device below its strategy threshold before the guard activates does not accumulate additional stop time during the freeze. Timers resume when the guard releases.

**Strategy threshold reference:**

| Strategy | Start threshold (average load) | Stop threshold (per device) | Absolute stop |
|---|---|---|---|
| As many as possible | 80% | 20% of device limit | 55W |
| As few as possible | 80% | 80% of device limit | 55W |
| Optimal | 80% | 25% of device limit | 55W |

---

## 12. Constants and Parameters Reference

### Fixed Hardware Constants

| Constant | Value | Description |
|---|---|---|
| Absolute minimum power | 55W | Below this a device is inefficient or will not start |
| Device setpoint tolerance | ±15W | Hardware accuracy; used by direction change guard |
| Device response time | ~4s | Time for a device to respond to a power command |
| Device start time | ~15s | Time for a newly started device to begin delivering power |
| Update cycle | 2s | Distribution recalculation interval |

### Distribution Constants

| Constant | Value | Description |
|---|---|---|
| `CONST_POWER_START` | 50W | Start pulse sent to a waking device |
| `CONST_FIXED` | 10% | Fixed base fraction of total power guaranteed to all active devices |
| Stop delay at threshold | 60s | Maximum delay before stopping a device at its strategy threshold |
| Stop delay at minimum | 2s | Minimum delay before stopping a device near 55W |

### Configurable Parameters

| Parameter | Non-expert | Expert default | Expert range |
|---|---|---|---|
| **Signal processing** | | | |
| EMA response speed | Slow / Medium / Fast | α = 0.3 | 0.0–1.0 |
| Rate limit threshold | Fixed 250W | 250W | Any watts |
| Rate limit damping factor | Fixed 75% | 0.75 | 0–1.0 |
| **Deadband** | | | |
| Seasonal mode | Normal / Winter / Summer / Minimal switching | — | — |
| Winter shift | Fixed −50W | −50W | Any watts |
| Summer shift | Fixed +50W | +50W | Any watts |
| Minimal switching minimum band | Fixed ±150W | ±150W | Any watts |
| Stddev multiplier | Fixed 1.0 | 1.0 | 0.0–3.0 |
| Gentle drift rate | Fixed 15W/cycle | 15W | Any W/cycle |
| **Hysteresis** | | | |
| Response profile | Precise / Moderate / Relaxed | Moderate (30s) | Duration in seconds |
| Peak detection threshold | Fixed 2×σ | 2.0 | 1.0–3.0 × σ |
| **Direction change guard** | | | |
| Guard trigger threshold | Fixed 250W | 250W | Any watts |
| Maximum guard duration | Fixed 8s | 8s | Any seconds |

### Removed Constants
The following constants from the original code are removed or replaced:

| Constant | Replacement |
|---|---|
| `CONST_POWER_JUMP` (100W history clear) | Removed — EMA handles naturally |
| `CONST_POWER_JUMP_HIGH` (250W damp to average) | Replaced by configurable rate limiter damping to EMA |
| `CONST_HIGH` (55% start threshold) | Replaced by strategy-aware 80% average load start threshold |
| `CONST_LOW` (15% stop threshold) | Replaced by strategy-aware per-device stop thresholds |
| Sign-change reset (force to zero) | Removed — direction change guard handles this |

---

## 13. Known Limitations

### Demand Spikes
Because a newly started device takes 15 seconds to deliver power, sudden large increases in demand (e.g. a dishwasher or washing machine starting) cannot always be covered immediately. The design mitigation is to start additional devices preemptively when active devices approach 80% average load, and to allow active devices to run at 100% during the gap, subject to fuse group hard limits. If demand exceeds the total capacity of all available devices, this cannot be resolved by the distribution logic alone.

### Device Non-Response
If a device fails to reach its commanded setpoint within the 8-second guard window, a warning is logged but no corrective action is taken beyond releasing the guard. Persistent non-response by a device is not currently detected or escalated automatically.
