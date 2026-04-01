import os
import sys

# 获取当前脚本所在目录
script_dir = os.path.dirname(os.path.abspath(__file__))
print(f"当前脚本所在目录: {script_dir}")

# 确保文件路径正确
file_path = os.path.join(script_dir, "my_strategy.py")
print(f"预期文件路径: {file_path}")

# 你的代码...
# -*- coding: utf-8 -*-
"""
量化回测策略主程序
功能：
- 每日自动下载股票数据（通过 yfinance）
- 运行简单均线交叉策略
- 生成日志报告
- 支持 --notify 参数发送微信 + 邮箱通知

作者：你的名字
最后更新：2026-03-25
"""

import backtrader as bt
import yfinance as yf
import datetime
import os
import sys
import argparse
import smtplib
import requests
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


def run_backtest(symbol="AAPL", start_date="2025-01-01", cash=100000.0):
    """运行回测并返回最终资金和日志内容"""
    # 创建 Cerebro 引擎
    cerebro = bt.Cerebro()
    cerebro.addstrategy(SimpleSMACross)
    cerebro.broker.setcash(cash)

    # 下载数据（yfinance 自动处理时区）
    data = yf.download(
        symbol,
        start=start_date,
        end=datetime.datetime.today().strftime("%Y-%m-%d"),
        progress=False
    )
    
    if data.empty:
        raise ValueError(f"❌ 无法获取 {symbol} 的数据，请检查代码或网络")

    # 转换为 Backtrader 数据格式
    data_feed = bt.feeds.PandasData(dataname=data)
    cerebro.adddata(data_feed)

    # 运行回测
    initial_value = cerebro.broker.getvalue()
    cerebro.run()
    final_value = cerebro.broker.getvalue()

    # 生成报告文本
    log_lines = []
    log_lines.append(f"📊 量化回测报告")
    log_lines.append(f"股票代码: {symbol}")
    log_lines.append(f"回测周期: {start_date} 至 {datetime.date.today()}")
    log_lines.append(f"初始资金: ${initial_value:.2f}")
    log_lines.append(f"最终资金: ${final_value:.2f}")
    log_lines.append(f"收益率: {(final_value / initial_value - 1) * 100:.2f}%")
    log_lines.append("=" * 40)
    log_lines.append("策略: 5日/20日均线交叉")

    return final_value, "\n".join(log_lines)


def send_wechat(title, content):
    """通过 Server 酱发送微信通知"""
    sckey = os.getenv("SCKEY")
    if not sckey:
        print("⚠️ 未配置 Server 酱 SCKEY，跳过微信通知")
        return
    url = f"https://sctapi.ftqq.com/{sckey}.send"
    data = {
        "title": title,
        "desp": content
    }
    try:
        resp = requests.post(url, data=data, timeout=10)
        if resp.json().get("code") == 0:
            print("✅ 微信通知已发送")
        else:
            print(f"❌ 微信通知失败: {resp.text}")
    except Exception as e:
        print(f"❌ 微信通知异常: {e}")


def send_email(subject, body):
    """发送邮件（支持 Gmail / QQ 邮箱）"""
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    to_email = os.getenv("TO_EMAIL")
    
    if not all([email_user, email_pass, to_email]):
        print("⚠️ 未配置邮箱信息，跳过邮件通知")
        return

    # 自动判断邮箱类型
    if "gmail.com" in email_user:
        smtp_server = "smtp.gmail.com"
        port = 587
    elif "qq.com" in email_user:
        smtp_server = "smtp.qq.com"
        port = 587
    elif "outlook.com" in email_user or "hotmail.com" in email_user:
        smtp_server = "smtp-mail.outlook.com"
        port = 587
    else:
        # 默认使用 Gmail 设置
        smtp_server = "smtp.gmail.com"
        port = 587

    msg = MIMEMultipart()
    msg['From'] = email_user
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        server = smtplib.SMTP(smtp_server, port)
        server.starttls()
        server.login(email_user, email_pass)
        server.sendmail(email_user, to_email, msg.as_string())
        server.quit()
        print("✅ 邮件已发送")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='量化回测策略')
    parser.add_argument('--symbol', type=str, default='600519.SS', help='股票代码 (默认: 贵州茅台)')
    parser.add_argument('--start', type=str, default='2025-01-01', help='回测开始日期')
    parser.add_argument('--cash', type=float, default=100000.0, help='初始资金')
    parser.add_argument('--notify', action='store_true', help='发送微信和邮箱通知')
    args = parser.parse_args()

    # 创建 logs 目录
    os.makedirs("logs", exist_ok=True)
    log_file = f"logs/run_{datetime.datetime.now().strftime('%Y%m%d')}.log"

    try:
        final_value, report_text = run_backtest(
            symbol=args.symbol,
            start_date=args.start,
            cash=args.cash
        )

        # 保存日志到文件
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(report_text)
        
        print(f"✅ 回测完成！报告已保存至: {log_file}")
        print(report_text)

        # 发送通知
        if args.notify:
            title = f"📈 量化回测报告 - {datetime.date.today()} | {args.symbol}"
            send_wechat(title, report_text)
            send_email(title, report_text)

    except Exception as e:
        error_msg = f"❌ 回测失败: {str(e)}"
        print(error_msg)
        # 即使失败也尝试通知
        if args.notify:
            send_wechat("🚨 量化回测失败", error_msg)
            send_email("🚨 量化回测失败", error_msg)