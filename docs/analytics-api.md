# analytics public API (summary from the analytics build; read the code docstrings for details)
Dates are `date`; unknown → None; arrays NaN internally.

bs: price(S,K,T,r,q,sigma,right); greeks(...)->{delta,gamma,vega(/vol pt),theta(/cal day),rho(/1%)}; delta; is_call; no_arb_bounds; implied_vol(...)->float|None
chain: ChainArrays.from_contracts(contracts, spot, asof, *, symbol, r, q, hv30, iv_rank, next_earnings); ChainArrays.from_columns(expiry=,strike=,right=,bid=,ask=,last=,volume=,oi=,iv=,delta=,gamma=,theta=,vega=,spot=,asof=,symbol=,osi=,src=,r,q,hv30,iv_rank,next_earnings,next_reaction); ca.earnings_before_expiry() (per expiry, reaction-day based) [for DB columns]; ChainArrays.from_snapshot(chain, now); option_row(contract, spot)->OptionRow
vol: hv, hv_series, hv_snapshot(closes)->{hv10..hv252}; parkinson(high,low,n=20); realized_forward(closes,21); iv_rank_percentile(history,current=None)->{iv_rank,iv_percentile,history_days,iv_high_52w,iv_low_52w}; atm_iv_by_expiry(ca); interp_total_variance; constant_maturity(rows,tenors,key); forward_vol; delta_skew_by_expiry(ca); skew_30(ca, atm30)
chainmetrics: compute_chain_metrics(chain, now) (METRIC_KEYS); metrics_from_arrays(ca, iv30_cboe); flow_totals(ca); expiry_infos(ca)->ExpiryInfo[]; straddle_rows(ca); expiry_term_rows(ca) (input to earnings.implied_earnings_move); by_strike(ca, expiry|None, prior_oi|None)->StrikeAgg[]; max_pain(ca,expiry); max_pain_all(ca); top_contracts(ca,20); oi_changes(ca, prior_oi, n, expiry)->{top_increases,top_decreases}; implied_moves(ca, earnings_date)->ImpliedMoves.rows; term_structure(ca)->TermPoint[]; smile(ca, expiry)->Skew.points; moneyness_masks(ca)
probability: prob_above, expected_call/put/spot, grid(S,T,σ,μ,n=2001), pop_ev_on_grid
strategies: Leg(kind,qty,right,strike,expiry,price,iv,mid); describe_legs; net_cost($); value_at(legs,S,at,r,q,iv_shift); position_greeks; naked_margin; TEMPLATES (29); strategy_templates()->StrategyTemplate[]; resolve_strikes(template, spot, strikes_for, rule, value, width_pct, delta_for); ScreenParams(dte_min,dte_max,min_oi,min_volume,max_spread_pct,require_bid,pricing,earnings_filter,delta_min,delta_max,max_width_steps,width_min_pct,width_max_pct,back_gap_min,back_gap_max,side); screen(id, ca, params)->ScreenResult|None (.rows(), .order_by(), .concat()); screen_universe(id, chains, params, sort_key, descending, limit=5000)->(rows,truncated); rank_trade_ideas(ca, params, n, strategies, families, seasonality)->{symbol,by_edge,by_win_rate}; SCREENER_IDS (21 ids of SPEC §7)
payoff: calculate(legs, spot, now, r, q, *, hv30, iv_shift, eval_date, price_range, points=121, dist_iv, asof)->CalcResult (legs must have price & iv resolved)
optbacktest: PriceProvider protocol (real_mid, iv_proxy; optional listed_expiries, listed_strikes, real_spread_pct); backtest_options(symbol, bars, template_id, *, provider, rate, div_yield, strike_rule, rule_value, width_pct, entry, target_dte, exit, start, end, qty, is_index)->BacktestResult (rate/div_yield float or callable(date)); straddle_backtest(...); straddle_wing_backtest(..., side, wing_pct); earnings_strategy_backtest(symbol, bars, events, *, provider, rate, div_yield, lookback, entry, exit, strategies)->EarningsStrategyResult; earnings_strategy_trades(..., strategy_id, ...)->BacktestTrade[]; EARNINGS_STRATEGY_IDS (30)
backtest: build_result, equity_stats, backtest_stock(bars_by_symbol, entry, exit, side, start, end, earnings_by_symbol, initial=10000)->BacktestResult
earnings: reaction_date, event_rows(events, bars, implied_moves=, iv30_by_date=)->EarningsEventRow[], earnings_stats, implied_earnings_move, implied_vs_hist_ratio
dividends: dividend_summary(symbol, divs, bars, price, asof)->Dividends; trailing_yield (q), frequency, annual_rate, growth, capture_rows, capture_stats, is_dividend_increase, next_dividend
stats: bar_arrays, returns_row, beta_corr, rsi/rsi_series, atr_pct, sma/sma_series, high_low, avg_volume, relative_volume, support_resistance, gap_signal, price_action->PriceAction, seasonality->Seasonality, vol_seasonality->months, monthly_returns, intraday_overnight

## Strategy screener row keys (all rows)
symbol, strategy, legs, expiry, back_expiry, dte, spot, strikes, price, price_natural, price_mid, pricing, cost, is_credit, max_profit, max_loss, breakevens, breakeven_low, breakeven_high, return_on_risk, annualized_return, pop, prob_max_profit, ev_iv, ev_hv, delta, gamma, theta, vega, iv, atm_iv, iv_rank, hv30, earnings_before_expiry, next_earnings, min_leg_oi, min_leg_volume, max_leg_spread_pct
+ covered-calls: static_return, static_annualized, if_called_return, if_called_annualized, downside_protection, prob_called
+ naked-puts: return_on_cash, return_on_cash_annualized, margin_req, return_on_margin, return_on_margin_annualized, prob_otm
+ calendars: front_iv, back_iv, iv_ratio, forward_vol, theta_ratio, est_max_profit
+ synthetic-long: synthetic_price_natural, synthetic_price_mid, parity_gap_natural, parity_gap_mid
Units: price* per share (+debit/−credit); $ fields per 1 contract per leg; max_loss positive magnitude, None = unlimited.

## Resolved ambiguities
- Earnings exit +n = close of reaction day + n trading days (0 accepted). Short premium (credit / long calendar) uses first expiry ≥ exit+2 cal days. Calendar back: screener nearest later expiry with gap 20–70d; earnings backtest next monthly after front.
- Lognormal σ = the expiry's ATM IV. EV undiscounted P/L at expiry.
- Return basis: debit; else finite max loss; else Reg-T naked margin; short stock 100%.
- Options-backtest equity = capital (peak basis) + cumulative P/L, non-compounded.
- Stock backtester signals fire on crossing into condition; max_drawdown negative fraction.
- Known: SPX/SPXW duplicate expiry/strike pairs are NOT de-duplicated by analytics — the services layer must de-dup (prefer SPXW PM-settled when both exist for the same expiry/strike) before building ChainArrays.
