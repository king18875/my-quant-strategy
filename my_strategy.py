# -*- coding: utf-8 -*-
"""
个人量化模拟交易系统 v6.0 - 全面适配 AKShare（A股专用）
✅ 数据源：AKShare（支持 600519.SS / 000858.SZ 等 A 股）
✅ 策略：RSI + MACD + 成交量 + 静态 PE（手动维护）
✅ 严格 T+1 限制
✅ 邮件通知 via 163（需设置环境变量）
✅ 生成 Excel + HTML 报告
"""

import backtrader as bt
import pandas as pd
import numpy as np
import os
import sys
import argparse
import datetime
import shutil
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# === 手动维护的静态 PE 字典（可按需扩展）===
# 来源：2025年报/2026Q1 估算，单位：倍
STATIC_PE_MAP = {
    '600519': 30.5,   # 贵州茅台
    '000858': 22.3,   # 五粮液
    '601318': 8.7,    # 中国平安
    '000001': 5.2,    # 平安银行
    '600036': 6.1,    # 招商银行
    '300750': 28.9,   # 宁德时代
}

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
        self.last_buy_date = {}  # T+1 记录

        # 指标字典
        self.rsi = {}
        self.macd = {}
        self.macd_signal = {}
        self.vol_ma5 = {}
        self.vol_ma20 = {}

        for d in self.datas:
            symbol = d._name
            clean_code = symbol.split('.')[0]
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
            # PE（从静态字典获取）
            pe_val = STATIC_PE_MAP.get(clean_code, np.nan)
            setattr(self, f'pe_{symbol}', pe_val)

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

        if self.last_rebalance is None or (current_date - self.last_rebalance).days >= self.p.rebalance_days:
            self.rebalance_portfolio(current_date)

    def rebalance_portfolio(self, current_date):
        scores = {}
        for d in self.datas:
            symbol = d._name
            clean_code = symbol.split('.')[0]
            rsi_val = self.rsi[symbol][0]
            macd_val = self.macd[symbol][0]
            signal_val = self.macd_signal[symbol][0]
            vol5 = self.vol_ma5[symbol][0]
            vol20 = self.vol_ma20[symbol][0]
            pe_val = getattr(self, f'pe_{symbol}', np.nan)

            score = 0

            # RSI 打分
            if rsi_val < 30:
                score += 1.0
            elif rsi_val > 70:
                score -= 0.5

            # MACD 金叉
            if macd_val > signal_val and self.macd[symbol][-1] <= self.macd_signal[symbol][-1]:
                score += 0.8

            # PE 估值（越低越好，仅当有值时）
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
                    self.last_buy_date[symbol] = current_date
            elif diff < 0:
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
        pass


# ========================
# 数据加载（使用 AKShare）
# ========================
def load_or_download_data(symbols, start_date, end_date, cache_dir="data"):
    import akshare as ak
    os.makedirs(cache_dir, exist_ok=True)
    datas = []

    for symbol in symbols:
        clean_code = symbol.split('.')[0]
        market_suffix = symbol.split('.')[1] if '.' in symbol else 'SS'

        cache_file = os.path.join(cache_dir, f"{clean_code}.csv")
        df = None

        # 尝试加载缓存
        if os.path.exists(cache_file):
            try:
                df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                if not df.empty and df.index[-1].date() >= (datetime.datetime.today() - datetime.timedelta(days=2)).date():
                    print(f"💾 使用缓存: {symbol}")
                else:
                    df = None
            except Exception as e:
                print(f"⚠️ 缓存读取失败 {symbol}: {e}")
                df = None

        # 下载新数据
        if df is None:
            print(f"🌐 通过 AKShare 下载: {symbol} ({clean_code})")
            try:
                df = ak.stock_zh_a_hist(
                    symbol=clean_code,
                    period="daily",
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                    adjust="qfq"
                )
                if df.empty:
                    raise ValueError("返回空数据")

                # 重命名列
                df.rename(columns={
                    '日期': 'date',
                    '开盘': 'open',
                    '收盘': 'close',
                    '最高': 'high',
                    '最低': 'low',
                    '成交量': 'volume'
                }, inplace=True)
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)

                # 保存缓存
                df.to_csv(cache_file)
                print(f"✅ 下载成功: {symbol}")
            except Exception as e:
                print(f"❌ {symbol} 下载失败: {e}")
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
        raise ValueError("无有效数据，请检查股票代码格式（如 600519.SS）")

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


def send_notification_email(report, recipients=None):
    sender = os.getenv("EMAIL_USER", "your_email@163.com")
    password = os.getenv("EMAIL_PASSWORD", "your_authorization_code")
    smtp_server = "smtp.163.com"
    smtp_port = 465

    if recipients is None:
        recipients = [sender]

    subject = f"📈 量化日报 | 净值: ¥{report['final_value']:,.2f} | 收益率: {report['total_return_pct']:.2f}%"
    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2>📊 个人量化模拟交易日报</h2>
        <p><strong>初始资金：</strong>¥{report['initial_value']:,.2f}</p>
        <p><strong>当前净值：</strong>¥{report['final_value']:,.2f}</p>
        <p><strong>总收益率：</strong>{report['total_return_pct']:.2f}%</p>
        <p><strong>最大回撤：</strong>{report['max_drawdown_pct']:.2f}%</p>
        <p><strong>风控状态：</strong>{"⚠️ 已触发清仓" if report['drawdown_triggered'] else "🟢 正常"}</p>
        <p>详细报告请查看附件或 GitHub Pages 页面。</p>
        <hr>
        <small>策略：四因子选股（RSI+MACD+PE+成交量） | 严格 T+1 限制 | 数据源：AKShare</small>
    </body>
    </html>
    """

    msg = MIMEText(body, 'html', 'utf-8')
    msg['From'] = Header(f"量化机器人 <{sender}>", 'utf-8')
    msg['To'] = Header("; ".join(recipients), 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print("📧 邮件通知已发送！")
        return True
    except Exception as e:
        print(f"❌ 邮件发送失败（不影响主程序）: {e}")
        return False


# ========================
# 主程序入口
# ========================
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='A股多因子量化回测系统（基于 AKShare）')
    parser.add_argument('--symbols', type=str, default='600519.SS,000858.SZ',
                        help='A股代码，格式：600519.SS（沪市）或 000858.SZ（深市），多个用逗号分隔')
    parser.add_argument('--start', type=str, default='2025-01-01', help='回测开始日期（YYYY-MM-DD）')
    parser.add_argument('--cash', type=float, default=100000.0, help='初始资金')
    parser.add_argument('--notify', action='store_true',
                        help='启用163邮箱通知（需设置 EMAIL_USER 和 EMAIL_PASSWORD 环境变量）')
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    today = datetime.date.today()
    os.makedirs("reports", exist_ok=True)
    excel_file = f"reports/report_{today}.xlsx"

    try:
        report = run_simulation(symbols, args.start, args.cash)
        export_to_excel(report, excel_file)

        # 尝试生成 HTML 报告（如果 utils/html_report 存在）
        try:
            from utils.html_report import generate_html_report
            html_content, _ = generate_html_report(report)
            html_file = f"reports/report_{today}.html"
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(html_content)
            shutil.copy(html_file, "reports/latest.html")
            print(f"🌐 HTML 报告已保存: {html_file}")
        except ImportError:
            print("ℹ️ 未找到 utils/html_report.py，跳过 HTML 报告生成")

        # 发送邮件
        if args.notify:
            send_notification_email(report)

        # 打印结果
        print("\n✅ 回测完成！")
        print(f"初始资金: ¥{report['initial_value']:,.2f}")
        print(f"当前净值: ¥{report['final_value']:,.2f}")
        print(f"总收益率: {report['total_return_pct']:.2f}%")
        print(f"最大回撤: {report['max_drawdown_pct']:.2f}%")

    except Exception as e:
        print(f"❌ 主程序错误: {e}", file=sys.stderr)
        sys.exit(1)
