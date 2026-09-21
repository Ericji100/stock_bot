import yfinance as yf
import pandas as pd
import sys

def get_backtest_report(symbol, strategy_mode, years=2, verbose=False):
    try:
        # 1. 抓取資料 (確保 auto_adjust=False 以對齊三竹原始 K 線)
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1500d", interval="1d", auto_adjust=False)
        
        if df.empty or len(df) < 250:
            return f"❌ {symbol} 資料量不足。"
        
        # 移除時區資訊
        df.index = df.index.tz_localize(None)

        # 2. 指標計算 (嚴格對齊三竹參數：21, 55, 55)
        df['MA21'] = df['Close'].rolling(window=21).mean()
        ema21 = df['Close'].ewm(span=21, adjust=False).mean()
        ema55 = df['Close'].ewm(span=55, adjust=False).mean()
        df['DIF'] = ema21 - ema55
        df['DEA'] = df['DIF'].ewm(span=55, adjust=False).mean()
        df['MACD_Hist'] = df['DIF'] - df['DEA']
        
        # 停損參考價：前三日最低
        df['Stop_Ref'] = df['Low'].rolling(window=3).min()

        # 交易變數
        in_position = False
        buy_price = 0
        fixed_stop_loss = 0
        reached_profit_target = False 
        needs_cooldown = False 
        
        trades = []
        stop_loss_count = 0 
        take_profit_count = 0 

        # 設定回測起始點
        start_idx = len(df) - (years * 252)
        if start_idx < 150: start_idx = 150
        
        test_start_date = df.index[start_idx].date()
        test_end_date = df.index[-1].date()

        if verbose: 
            print(f"\n📝 【{symbol}】交易明細 (策略：{'MA21 突破' if strategy_mode == 'A' else 'MACD 精確突破'})")
            print("-" * 75)

        # 3. 核心迴圈邏輯
        for i in range(start_idx, len(df)):
            today = df.iloc[i]
            yesterday = df.iloc[i-1]
            current_date = df.index[i].date()
            
            # 冷卻狀態監測 (碰線即解除)
            if today['Low'] <= today['MA21']:
                needs_cooldown = False

            if not in_position:
                trigger_entry = False
                
                # --- 策略 A: 純 MA21 突破 ---
                if strategy_mode == "A":
                    if (yesterday['Close'] < yesterday['MA21']) and (today['Close'] > today['MA21']):
                        trigger_entry = True

                # --- 策略 B: MACD 翻紅回測再突破 ---
                else:
                    if today['MACD_Hist'] > 0:
                        df_past = df.iloc[:i]
                        green_days = df_past[df_past['MACD_Hist'] <= 0]
                        if not green_days.empty:
                            red_start_date = green_days.index[-1] + pd.Timedelta(days=1)
                            df_red_zone = df.loc[red_start_date : df.index[i-1]]
                            if not df_red_zone.empty:
                                touch_days = df_red_zone.index[df_red_zone['Low'] <= df_red_zone['MA21']]
                                if len(touch_days) > 0:
                                    touch_pos = df_red_zone.index.get_loc(touch_days[-1])
                                    df_pre_touch = df_red_zone.iloc[:touch_pos]
                                    if not df_pre_touch.empty:
                                        breakout_high = df_pre_touch['High'].max()
                                        if not needs_cooldown and yesterday['Close'] <= breakout_high and today['Close'] > breakout_high and today['Close'] > today['MA21']:
                                            trigger_entry = True

                if trigger_entry:
                    buy_price = today['Close']
                    fixed_stop_loss = today['Stop_Ref']
                    in_position = True
                    reached_profit_target = False
                    needs_cooldown = True 
                    if verbose:
                        print(f"✅ [{current_date}] 入場 @ {buy_price:.2f} (初始停損: {fixed_stop_loss:.2f})")

            elif in_position:
                # 持股期間提示 (僅策略 B 顯示)
                if strategy_mode == "B":
                    if today['MACD_Hist'] > 0:
                        df_past = df.iloc[:i]
                        green_days = df_past[df_past['MACD_Hist'] <= 0]
                        if not green_days.empty:
                            red_start_date = green_days.index[-1] + pd.Timedelta(days=1)
                            df_red_zone = df.loc[red_start_date : df.index[i-1]]
                            if not df_red_zone.empty:
                                touch_days = df_red_zone.index[df_red_zone['Low'] <= df_red_zone['MA21']]
                                if len(touch_days) > 0:
                                    touch_pos = df_red_zone.index.get_loc(touch_days[-1])
                                    df_pre_touch = df_red_zone.iloc[:touch_pos]
                                    if not df_pre_touch.empty:
                                        breakout_high = df_pre_touch['High'].max()
                                        if not needs_cooldown and yesterday['Close'] <= breakout_high and today['Close'] > breakout_high and today['Close'] > today['MA21']:
                                            needs_cooldown = True
                                            if verbose:
                                                print(f"🔔 [{current_date}] ⚠️ 回測後再突破 (持股中未加碼) @ {today['Close']:.2f}")

                # 出場邏輯
                if not reached_profit_target and today['High'] >= buy_price * 1.05:
                    reached_profit_target = True

                is_stop_loss = today['Close'] < fixed_stop_loss
                is_ma_exit = reached_profit_target and (today['Close'] < today['MA21'])

                if is_stop_loss or is_ma_exit:
                    sell_price = today['Close']
                    profit = (sell_price - buy_price) / buy_price * 100
                    trades.append(profit)
                    
                    reason = "🟢停利" if profit > 0 else "🔴停損"
                    if profit > 0: take_profit_count += 1
                    else: stop_loss_count += 1
                    
                    if verbose:
                        print(f"👉 [{current_date}] {reason} 出場 @ {sell_price:.2f} | 損益: {profit:.2f}%")
                    
                    in_position = False
                    needs_cooldown = False 

        # 4. 結算統計報告
        if not trades: 
            return f"📭 {symbol} 在此期間無交易訊號。"
        
        win_rate = (len([t for t in trades if t > 0]) / len(trades)) * 100
        total_profit = sum(trades)

        report = (
            f"\n📊 **{symbol} 績效回測報告 (策略 {'B' if strategy_mode=='B' else 'A'})**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"● 測試期間：{test_start_date} ~ {test_end_date}\n"
            f"● 總交易次數：{len(trades)} 次\n"
            f"● 停利次數(🟢)：{take_profit_count} 次\n"
            f"● 停損次數(🔴)：{stop_loss_count} 次\n"
            f"● 勝率：{win_rate:.1f}%\n"
            f"━━━━━━━━━━━━━━━\n"
            f"● 累計報酬：{total_profit:.2f}%\n"
            f"● 最大單筆獲利：{max(trades):.1f}%\n"
            f"● 最大單筆虧損：{min(trades):.1f}%\n"
            f"━━━━━━━━━━━━━━━"
        )
        return report

    except Exception as e:
        return f"❌ 系統錯誤: {str(e)}"

if __name__ == "__main__":
    code = input("請輸入代碼 (例如 3030.TW): ").upper()
    print("\n請選擇策略模式：\nA) MA21 突破\nB) 強勢上漲後回測突破")
    choice = input("輸入 A 或 B: ").upper()

    if choice in ["A", "B"]:
        print(get_backtest_report(code.upper(), choice, years=3, verbose=True))
    else:
        print("❌ 無效選擇")
