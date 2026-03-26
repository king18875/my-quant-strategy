# -*- coding: utf-8 -*-
"""
量化回测策略主程序
功能：
- 每日自动下载股票数据（通过 yfinance）
- 运行简单均线交叉策略
- 生成中文日志报告
- 支持 --notify 参数发送邮箱通知（仅邮件，无微信）

作者：你的名字
最后更新：2026-03-26
"""

import backtrader as bt
import yfinance as yf
import datetime
import os
import sys
import argparse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


class SimpleSMACross(bt.Strategy):
    params = (
        ('fast', 5),
        ('slow', 20),
    )

    def __init__(self):
        self.fast_ma = bt.indicators.SMA(period=self.p.fast)
        self.slow_ma = bt.indicators.SMA(period=self.p.slow)
        self.crossover = bt.indicators.CrossOver(self.fast_ma, self.slow_ma)

    def next(self):
        if not self.position:
            if self.crossover > 0:
                self.buy()
        elif self.crossover < 0:
            self.close()


def run_backtest(symbol="600519.SS", start_date="2025-01-01", cash=100000.0):
    """运行回测并返回最终资金和中文日志内容"""
    cerebro = bt.Cerebro()
    cerebro.addstrategy(SimpleSMACross)
    cerebro.broker.setcash(cash)

    data = yf.download(
        symbol,
        start=start_date,
        end=datetime.datetime.today().strftime("%Y-%m-%d"),
        progress=False
    )
    
    if data.empty:
        raise ValueError(f"❌ 无法获取 {symbol} 的数据，请检查股票代码或网络连接")

    data_feed = bt.feeds.PandasData(dataname=data)
    cerebro.adddata(data_feed)

    initial_value = cerebro.broker.getvalue()
    cerebro.run()
    final_value = cerebro.broker.getvalue()

    log_lines = []
    log_lines.append("📊 量化回测报告")
    log_lines.append(f"股票代码：{symbol}")
    log_lines.append(f"回测周期：{start_date} 至 {datetime.date.today()}")
    log_lines.append(f"初始资金：¥{initial_value:,.2f}")
    log_lines.append(f"最终资金：¥{final_value:,.2f}")
    log_lines.append(f"总收益率：{(final_value / initial_value - 1) * 100:.2f}%")
    log_lines.append("=" * 40)
    log_lines.append("策略说明：5日与20日均线金叉/死叉交易策略")
    log_lines.append("• 金叉（短均上穿长均）→ 买入")
    log_lines.append("• 死叉（短均下穿长均）→ 卖出")

    return final_value, "\n".join(log_lines)


def send_email(subject, body):
    """发送中文邮件（专为 163 邮箱优化）"""
    # 安全获取环境变量：转字符串 + 去空格
    email_user = str(os.getenv("EMAIL_USER", "")).strip()
    email_pass = str(os.getenv("EMAIL_PASS", "")).strip()
    to_email = str(os.getenv("TO_EMAIL", "")).strip()

    if not all([email_user, email_pass, to_email]):
        print("⚠️ 未配置完整的邮箱信息（EMAIL_USER / EMAIL_PASS / TO_EMAIL），跳过邮件通知")
        return

    # 163 邮箱固定配置（SSL + 端口 465）
    smtp_server = "smtp.163.com"
    port = 465

    msg = MIMEMultipart()
    msg['From'] = email_user
    msg['To'] = to_email
    msg['Subject'] = subject

    # 关键：指定 'utf-8' 编码，确保中文不乱码
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        with smtplib.SMTP_SSL(smtp_server, port) as server:
            server.login(email_user, email_pass)
            server.sendmail(email_user, to_email, msg.as_string())
        print("✅ 中文邮件已成功发送到", to_email)
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='量化回测策略（中文报告）')
    parser.add_argument('--symbol', type=str, default='600519.SS', help='股票代码 (默认: 贵州茅台)')
    parser.add_argument('--start', type=str, default='2025-01-01', help='回测开始日期')
    parser.add_argument('--cash', type=float, default=100000.0, help='初始资金（人民币）')
    parser.add_argument('--notify', action='store_true', help='发送中文邮箱通知')
    args = parser.parse_args()

    os.makedirs("logs", exist_ok=True)
    log_file = f"logs/run_{datetime.datetime.now().strftime('%Y%m%d')}.log"

    try:
        final_value, report_text = run_backtest(
            symbol=args.symbol,
            start_date=args.start,
            cash=args.cash
        )

        with open(log_file, "w", encoding="utf-8") as f:
            f.write(report_text)
        
        print(f"✅ 回测完成！中文报告已保存至: {log_file}")
        print(report_text)

        if args.notify:
            title = f"📈 量化回测报告 - {datetime.date.today()} | {args.symbol}"
            send_email(title, report_text)

    except Exception as e:
        error_msg = f"❌ 回测失败: {str(e)}"
        print(error_msg, file=sys.stderr)
        if args.notify:
            send_email("🚨 量化回测失败", error_msg)
        sys.exit(1)
