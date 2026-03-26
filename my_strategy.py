# -*- coding: utf-8 -*-
"""
个人量化模拟交易系统 v4.0 - 小白友好版
✅ RSI + MACD + PE + 成交量 四因子
✅ 真实 T+1 限制（今日买，明日才能卖）
✅ 生成 HTML 图表 + Excel 报告
✅ 支持 GitHub Actions 自动运行
"""

import backtrader as bt
import yfinance as yf
import pandas as pd
import numpy as np
import os
import sys
import argparse
import datetime
import shutil

# === 风控常量 ===
MAX_POSITION_PER_STOCK = 0.30
MAX_TOTAL_POSITION = 0.90
MAX_DRAWDOWN_STOP = 0.15


class MultiFactorStrategy(bt.Strategy):
    params = (
        ('rebalance_days', 5),
        ('rsi_period', 14),
        ('macd_fast', 12),
        ('macd_slow', 26),
        ('macd_signal', 9),
    )

    def __init__(self):
        self.last_rebalance = None
        self.peak_value = self.broker.getvalue()
        self.drawdown_triggered = False
        self.last_buy_date = {}  # 记录每只股票最近买入日期（用于T+1）

        # 指标字典
        self.rsi = {}
        self.macd = {}
        self.macd_signal = {}
        self.pe_ratio = {}
        self.vol_ma5 = {}
        self.vol_ma20 = {}

        for d in self.datas:
            symbol = d._name
            # RSI
            self.rsi[symbol] = bt.indicators.RSI(d.close, period=self.p.rsi_period)
            # MACD
            macd_ind = bt.indicators.MACD(
                d.close,
                period_me1=self.p.macd_fast,
                period_me2=self.p.macd_slow,
                period_signal=self.p.macd_signal
            )
            self.macd[symbol] = macd_ind.macd
            self.macd_signal[symbol] = macd_ind.signal
            # 成交量 MA5 / MA20
            self.vol_ma5[symbol] = bt.indicators.SMA(d.volume, period=5)
            self.vol_ma20[symbol] = bt.indicators.SMA(d.volume, period=20)
            # PE（从 yfinance 获取）
            try:
                yf_symbol = symbol.replace('.SS', '.SH')
                ticker = yf.Ticker(yf_symbol)
                pe = ticker.info.get('trailingPE', np.nan)
            except:
                pe = np.nan
            self.pe_ratio[symbol] = pe

    def next(self):
        current_date = self.datas[0].datetime.date(0)
        current_value = self.broker.getvalue()
        self.peak_value = max(self.peak_value, current_value)
        drawdown = (self.peak_value - current_value) / self.peak_value
        if drawdown > MAX_DRAWDOWN_STOP:
            self.drawdown_triggered = True
            for d in self.datas:
                if self.getposition(d).size > 0:
                    self.close(d)
            return
        else:
            self.drawdown_triggered = False

        # 调仓逻辑（每5天一次）
        if self.last_rebalance is None or (current_date - self.last_rebalance).days >= self.p.rebalance_days:
            self.rebalance_portfolio(current_date)

    def rebalance_portfolio(self, current_date):
        scores = {}
        for d in self.datas:
            symbol = d._name
            rsi_val = self.rsi[symbol][0]
            macd_val = self.macd[symbol][0]
            signal_val = self.macd_signal[symbol][0]
            vol5 = self.vol_ma5[symbol][0]
            vol20 = self.vol_ma20[symbol][0]
            pe_val = self.pe_ratio[symbol]

            score = 0

            # RSI 打分
            if rsi_val < 30:
                score += 1.0
            elif rsi_val > 70:
                score -= 0.5

            # MACD 金叉
            if macd_val > signal_val and self.macd[symbol][-1] <= self.macd_signal[symbol][-1]:
                score += 0.8

            # PE 估值（越低越好）
            if not np.isnan(pe_val) and pe_val > 0 and pe_val < 100:
                pe_score = max(0, 1.0 - (pe_val / 30))
                score += pe_score * 0.5

            # 成交量因子
            if vol20 > 0:
                vol_ratio = vol5 / vol20
                if vol_ratio > 1.2:
                    score += 0.7
                elif vol_ratio < 0.8:
                    score -= 0.3

            scores[symbol] = score

        total_score = sum(v for v in scores.values() if v > 0)
        if total_score <= 0:
            for d in self.datas:
                if self.getposition(d).size > 0:
                    self.close(d)
            return

        weights = {s: max(0, scores[s]) / total_score for s in scores}
        cash = self.broker.getcash()

        for d in self.datas:
            symbol = d._name
            current_value = self.getposition(d).size * d.close[0]
            target_value = min(cash * weights.get(symbol, 0), self.broker.getvalue() * MAX_POSITION_PER_STOCK)
            diff = target_value - current_value

            if diff > 0:
                size = int(diff / d.close[0])
                if size > 0:
                    self.buy(data=d, size=size)
                    self.last_buy_date[symbol] = current_date  # 记录买入日期
            elif diff < 0:
                # ✅ T+1 检查：今天不能卖“今天刚买的”
                can_sell = True
                if symbol in self.last_buy_date:
                    buy_date = self.last_buy_date[symbol]
                    if (current_date - buy_date).days < 1:
                        can_sell = False
                if can_sell:
                    size = int(-diff / d.close[0])
                    if size > 0:
                        self.sell(data=d, size=min(size, self.getposition(d).size))

        self.last_rebalance = current_date

    def notify_order(self, order):
        pass  # 无需额外处理


def load_or_download_data(symbols, start_date, end_date, cache_dir="data"):
    os.makedirs(cache_dir, exist_ok=True)
    datas = []

    for symbol in symbols:
        cache_file = os.path.join(cache_dir, f"{symbol.replace('.', '_')}.csv")
        df = None

        if os.path.exists(cache_file):
            try:
                df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                if not df.empty and df.index[-1].date() >= (datetime.datetime.today() - datetime.timedelta(days=2)).date():
                    print(f"💾 使用缓存: {symbol}")
                else:
                    df = None
            except:
                df = None

        if df is None:
            print(f"🌐 下载: {symbol}")
            try:
                yf_symbol = symbol.replace('.SS', '.SH')
                df = yf.download(yf_symbol, start=start_date, end=end_date, progress=False)
                if not df.empty:
                    df.to_csv(cache_file)
            except Exception as e:
                print(f"⚠️ {symbol} 下载失败: {e}")
                continue

        if df is not None and not df.empty:
            datas.append((symbol, df))
        else:
            print(f"❌ 跳过: {symbol}")

    return datas


def run_simulation(symbols, start_date, cash=100000.0):
    end_date = datetime.datetime.today().strftime("%Y-%m-%d")
    datas = load_or_download_data(symbols, start_date, end_date)

    if not datas:
        raise ValueError("无有效数据")

    cerebro = bt.Cerebro()
    cerebro.addstrategy(MultiFactorStrategy)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=0.001)

    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='returns')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')

    for symbol, df in datas:
        data_feed = bt.feeds.PandasData(dataname=df, name=symbol)
        cerebro.adddata(data_feed)

    initial_value = cerebro.broker.getvalue()
    results = cerebro.run()
    final_value = cerebro.broker.getvalue()

    strategy = results[0]
    returns = strategy.analyzers.returns.get_analysis()
    dd_info = strategy.analyzers.drawdown.get_analysis()

    dates = list(returns.keys())
    values = [initial_value * (1 + r) for r in np.cumsum([returns[d] for d in dates])]
    nav_df = pd.DataFrame({'date': dates, 'nav': values})

    positions = {}
    for d in cerebro.datas:
        size = strategy.getposition(d).size
        if size != 0:
            positions[d._name] = {
                'shares': size,
                'price': d.close[0],
                'value': size * d.close[0]
            }

    report = {
        'initial_value': initial_value,
        'final_value': final_value,
        'total_return_pct': (final_value / initial_value - 1) * 100,
        'max_drawdown_pct': dd_info.max.drawdown,
        'positions': positions,
        'symbols': [d._name for d in cerebro.datas],
        'nav_df': nav_df,
        'drawdown_triggered': strategy.drawdown_triggered,
    }
    return report


def export_to_excel(report, filename):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with pd.ExcelWriter(filename, engine='openpyxl') as writer:
        report['nav_df'].to_excel(writer, sheet_name='净值曲线', index=False)
        if report['positions']:
            pos_df = pd.DataFrame.from_dict(report['positions'], orient='index')
            pos_df.to_excel(writer, sheet_name='当前持仓')
        summary = pd.DataFrame({
            '指标': ['初始资金', '当前净值', '总收益率(%)', '最大回撤(%)'],
            '值': [
                report['initial_value'],
                report['final_value'],
                report['total_return_pct'],
                report['max_drawdown_pct']
            ]
        })
        summary.to_excel(writer, sheet_name='摘要', index=False)
    print(f"📊 Excel 报告已保存: {filename}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='多因子量化回测系统')
    parser.add_argument('--symbols', type=str, default='600519.SS,000858.SZ',
                        help='A股代码用 .SS（沪市）或 .SZ（深市）')
    parser.add_argument('--start', type=str, default='2025-01-01', help='回测开始日期')
    parser.add_argument('--cash', type=float, default=100000.0, help='初始资金')
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    today = datetime.date.today()

    os.makedirs("reports", exist_ok=True)
    excel_file = f"reports/report_{today}.xlsx"

    try:
        report = run_simulation(symbols, args.start, args.cash)
        export_to_excel(report, excel_file)

        # 生成 HTML 报告
        from utils.html_report import generate_html_report
        html_content, _ = generate_html_report(report)

        html_file = f"reports/report_{today}.html"
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"🌐 HTML 报告已保存: {html_file}")

        # 创建 latest.html
        latest_file = "reports/latest.html"
        shutil.copy(html_file, latest_file)
        print(f"🔗 已更新最新报告: {latest_file}")

        # 打印简要结果
        print("\n✅ 回测完成！")
        print(f"初始资金: ¥{report['initial_value']:,.2f}")
        print(f"当前净值: ¥{report['final_value']:,.2f}")
        print(f"总收益率: {report['total_return_pct']:.2f}%")
        print(f"最大回撤: {report['max_drawdown_pct']:.2f}%")

    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)
