# Copy Trade

Mirror one broker account's positions onto another account automatically.

## What it does

The copy trade agent watches a **leader** account's live positions and keeps a
**follower** account in sync by placing market orders whenever the leader's
portfolio changes. It is broker-agnostic — any two connector profiles can be
paired (e.g. live Alpaca → paper Alpaca, Tiger live → Alpaca paper).

All orders go through the existing mandate gate and order_guard safety stack —
the copy trade engine never bypasses safety checks.

## Quick start

```
1. setup_copy_trade(leader_profile_id="alpaca-live", follower_profile_id="alpaca-paper", scale_ratio=0.5)
   → returns config_id, e.g. "ct_a3f9b2c1d4e5"

2. run_copy_trade_cycle(config_id="ct_a3f9b2c1d4e5", dry_run=true)
   → preview what would be traded without placing orders

3. run_copy_trade_cycle(config_id="ct_a3f9b2c1d4e5")
   → execute one sync cycle (live orders)

4. get_copy_trade_status()
   → list all configs and recent cycle history

5. stop_copy_trade(config_id="ct_a3f9b2c1d4e5")
   → disable (or permanently delete with permanent=true)
```

## Key parameters

| Parameter | Tool | Description |
|-----------|------|-------------|
| `leader_profile_id` | setup | Account to copy FROM |
| `follower_profile_id` | setup | Account to copy INTO |
| `scale_ratio` | setup | `follower qty = leader qty × ratio` (default 1.0, e.g. 0.1 = 10% of leader) |
| `max_order_notional` | setup | USD cap per single order (e.g. 1000.0) |
| `dry_run` | run cycle | Preview orders without placing them |
| `permanent` | stop | Delete config permanently vs just disabling |

## How positions are synced

Each cycle:
1. Fetch leader positions → `{AAPL: 100, NVDA: 50}`
2. Fetch follower positions → `{AAPL: 40}`
3. Compute delta with `scale_ratio=0.5`:
   - AAPL: target=50, current=40 → **BUY 10**
   - NVDA: target=25, current=0 → **BUY 25**
4. Place market orders on follower account

When the leader exits a position (qty → 0), the follower also sells.

## Scheduling continuous sync

To keep the follower in sync during market hours, instruct the scheduler:

> "Run run_copy_trade_cycle for config ct_a3f9b2c1d4e5 every 5 minutes
>  between 09:30 and 16:00 ET on weekdays"

The scheduler (via `VIBE_TRADING_ENABLE_SCHEDULER=1`) can automate this.

## Safety notes

- The follower account must have a **committed mandate** before live orders
  are placed. Paper accounts work without a mandate.
- Use `dry_run=true` before your first live cycle to verify the expected trades.
- `scale_ratio` can be set below 1.0 to reduce position size risk (e.g. 0.1
  for 10% of the leader's full size).
- `max_order_notional` limits exposure per symbol per cycle.
- `stop_copy_trade` immediately halts future cycles; it does NOT unwind
  existing follower positions — flatten those manually if needed.

## Example conversation

> User: "Set up copy trading — mirror my Alpaca live account into paper trading
>         with half the position sizes"
>
> Agent:
> 1. `list_trading_connectors()` → finds "alpaca-live" and "alpaca-paper"
> 2. `setup_copy_trade(leader="alpaca-live", follower="alpaca-paper", scale_ratio=0.5)`
> 3. `run_copy_trade_cycle(config_id="ct_...", dry_run=true)` → shows preview
> 4. Confirms with user, then `run_copy_trade_cycle(config_id="ct_...")` to execute
