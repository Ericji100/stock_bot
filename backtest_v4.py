import yfinance as yf
import pandas as pd
import sys

def get_backtest_report(symbol, strategy_input, max_positions=3, start_date="2018-01-01", verbose=False):
    try:
        # 1. 抓取資料 (從 2018 年開始)
        ticker = yf.Ticker(symbol)
        # 使用 start 參數指定起始時間
        df = ticker.history(start=start_date, interval="1d", auto_adjust=False)
        print(f"DEBUG: 抓取到的資料起始日期為: {df.index[0].date()}，總筆數: {len(df)}")
        
        if df.empty:
            return f"❌ 找不到 {symbol} 的資料，請確認代碼（如 006208.TW）。"
        
        if len(df) < 60:
            return f"❌ {symbol} 資料量不足（僅抓到 {len(df)} 筆），無法計算指標。"
        
        df.index = df.index.tz_localize(None)
        
        # 抓取股息歷史
        all_dividends = ticker.dividends
        if not all_dividends.empty:
            all_dividends.index = all_dividends.index.tz_localize(None)

        # 2. 指標計算
        df['MA21'] = df['Close'].rolling(window=21).mean()
        ema21 = df['Close'].ewm(span=21, adjust=False).mean()
        ema55 = df['Close'].ewm(span=55, adjust=False).mean()
        df['DIF'] = ema21 - ema55
        df['DEA'] = df['DIF'].ewm(span=55, adjust=False).mean()
        df['MACD_Hist'] = df['DIF'] - df['DEA']
        df['Stop_Ref'] = df['Low'].rolling(window=3).min()

        # --- 變數初始化 ---
        active_positions = []
        trades_history = []
        total_dividends_received = 0
        can_trade_c = True
        needs_cooldown_b = False

        selected_strategies = [s.strip() for s in strategy_input.upper().split('+')]
        
        # 從有指標的第一天開始跑 (避開前期的 NaN)
        start_idx = 100 
        test_start_date = df.index[start_idx].date()
        test_end_date = df.index[-1].date()

        if verbose: 
            print(f"\n📝 【{symbol}】長期回測 (起始：{test_start_date} | 模式：{strategy_input})")
            print("-" * 85)

        # 3. 核心迴圈
        for i in range(start_idx, len(df)):
            today = df.iloc[i]
            yesterday = df.iloc[i-1]
            current_date = df.index[i].date()
            current_dt = df.index[i]
            
            # 狀態更新
            if yesterday['MACD_Hist'] <= 0 and today['MACD_Hist'] > 0: can_trade_c = True
            if today['Low'] <= today['MA21']: needs_cooldown_b = False

            # --- A. 出場與股息檢查 ---
            still_holding = []
            for pos in active_positions:
                # 持有期間領股息
                if not all_dividends.empty and current_dt in all_dividends.index:
                    div_amount = all_dividends.loc[current_dt]
                    div_yield = (div_amount / pos['buy_price']) * 100
                    total_dividends_received += div_yield
                    if verbose:
                        print(f"💰 [{current_date}] 領取股息: {div_amount:.2f} (單筆收益約 {div_yield:.2f}%)")

                # 出場邏輯
                if not pos['reached_target'] and today['High'] >= pos['buy_price'] * 1.05:
                    pos['reached_target'] = True

                is_stop_loss = today['Close'] < pos['fixed_stop_loss']
                is_ma_exit = pos['reached_target'] and (today['Close'] < today['MA21'])

                if is_stop_loss or is_ma_exit:
                    profit = (today['Close'] - pos['buy_price']) / pos['buy_price'] * 100
                    trades_history.append({'strategy': pos['strategy'], 'profit': profit})
                    if profit < 0 and pos['strategy'] == "C": can_trade_c = False
                    if verbose:
                        reason = "🟢停利" if profit > 0 else "🔴停損"
                        print(f"👉 [{current_date}] {reason} 出場 ({pos['strategy']}) @ {today['Close']:.2f} | 價差: {profit:.2f}%")
                else:
                    still_holding.append(pos)
            active_positions = still_holding

            # --- B. 入場邏輯 ---
            if len(active_positions) < max_positions:
                for mode in selected_strategies:
                    if len(active_positions) >= max_positions: break
                    trigger = False
                    if mode == "A":
                        if (yesterday['Close'] < yesterday['MA21']) and (today['Close'] > today['MA21']): trigger = True
                    elif mode == "B":
                        if today['MACD_Hist'] > 0 and not needs_cooldown_b:
                            df_p = df.iloc[:i]
                            green = df_p[df_p['MACD_Hist'] <= 0]
                            if not green.empty:
                                red_s = green.index[-1] + pd.Timedelta(days=1)
                                df_rz = df.loc[red_s : df.index[i-1]]
                                if not df_rz.empty and (df_rz['Low'] <= df_rz['MA21']).any():
                                    if today['Close'] > df_rz['High'].max() and today['Close'] > today['MA21']:
                                        trigger = True; needs_cooldown_b = True
                    elif mode == "C":
                        if today['MACD_Hist'] < 0 and can_trade_c:
                            if (yesterday['Close'] < yesterday['MA21']) and (today['Close'] > today['MA21']): trigger = True

                    if trigger:
                        active_positions.append({
                            'strategy': mode, 'buy_price': today['Close'],
                            'fixed_stop_loss': today['Stop_Ref'], 'reached_target': False
                        })
                        if verbose:
                            print(f"✅ [{current_date}] 入場 ({mode}) @ {today['Close']:.2f} (部位: {len(active_positions)}/{max_positions})")

        # 4. 結算統計
        if not trades_history: return f"📭 {symbol} 自 2018 以來無成交紀錄。"
        
        profits = [t['profit'] for t in trades_history]
        total_cap_gain = sum(profits)
        real_total_return = total_cap_gain + total_dividends_received

        report = (
            f"\n📊 **{symbol} 長期組合績效報告 (2018-至今)**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"● 測試期間：{test_start_date} ~ {test_end_date}\n"
            f"● 最大部位限制：{max_positions}\n"
            f"● 總成交次數：{len(profits)} 次\n"
            f"● 停利次數(🟢)：{len([p for p in profits if p > 0])} 次\n"
            f"● 停損次數(🔴)：{len([p for p in profits if p <= 0])} 次\n"
            f"● 勝率：{(len([p for p in profits if p > 0])/len(profits)*100):.1f}%\n"
            f"━━━━━━━━━━━━━━━\n"
            f"● 累計價差報酬：{total_cap_gain:.2f}%\n"
            f"● 累計領取股息：{total_dividends_received:.2f}%\n"
            f"● **實際真實總報酬：{real_total_return:.2f}%** 🔥\n"
            f"━━━━━━━━━━━━━━━\n"
        )
        return report

    except Exception as e: return f"❌ 系統錯誤: {str(e)}"

if __name__ == "__main__":
    code = input("代碼 (例如 3030.TW): ").upper() or "3030.TW"
    mode_input = input("選擇策略組合 (A, B, C, A+B, B+C): ").upper() or "A+B"
    max_pos = int(input("最大持股數量 (1-5): ") or "3")
    
    # 直接執行，起始日期設為 2018-01-01
    print(get_backtest_report(code, mode_input, max_positions=max_pos, start_date="2018-01-01", verbose=True))