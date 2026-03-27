import backtrader as bt
import akshare as ak
import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import quantstats as qs

# ================= 1. 全局配置区 =================
# 这里的参数在 GitHub 环境变量中配置，本地运行若没配置则使用默认值
EMAIL_USER = os.getenv("EMAIL_USER", "你的QQ邮箱@qq.com")
EMAIL_PASS = os.getenv("EMAIL_PASS", "你的SMTP授权码")
EMAIL_TO = os.getenv("EMAIL_TO", "你的接收邮箱@qq.com")

# 回测时间设置 (GitHub Actions 建议只跑最近 6 个月，防止超时)
END_DATE = datetime.datetime.now().strftime("%Y%m%d")
START_DATE = (datetime.datetime.now() - datetime.timedelta(days=180)).strftime("%Y%m%d")

STOCK_POOL = ["600519", "300750", "000333", "601318", "002415"]

# ================= 2. 数据获取模块 =================
def get_stock_data(symbol):
    try:
        print(f"📥 正在下载 {symbol} 数据...")
        # 使用 akshare 下载前复权数据
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=START_DATE, end_date=END_DATE, adjust="qfq")
        if df.empty: return None
        
        # 数据清洗与格式化
        df.rename(columns={"日期": "datetime", "开盘": "open", "最高": "high", 
                           "最低": "low", "收盘": "close", "成交量": "volume"}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        return df
    except Exception as e:
        print(f"❌ {symbol} 数据获取失败: {e}")
        return None

# ================= 3. 策略逻辑 (简化版) =================
class SimpleStrategy(bt.Strategy):
    params = (('ma_short', 10), ('ma_long', 30),)

    def __init__(self):
        self.ma_short = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.ma_short)
        self.ma_long = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.ma_long)
        self.crossover = bt.indicators.CrossOver(self.ma_short, self.ma_long)

    def next(self):
        if not self.position:
            if self.crossover > 0: # 金叉买入
                print(f'📈 买入: {self.data._name} @ {self.data.close[0]:.2f}')
                self.order = self.buy()
        else:
            if self.crossover < 0: # 死叉卖出
                print(f'📉 卖出: {self.data._name} @ {self.data.close[0]:.2f}')
                self.order = self.close()

# ================= 4. 报表生成与邮件发送 =================
def send_email_report(stats, filename):
    try:
        print("📧 正在发送邮件报告...")
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = EMAIL_TO
        msg['Subject'] = f"量化策略日报 - {datetime.date.today()}"

        # 邮件正文
        body = f"你好，\n\n今日策略回测已完成。请查看附件中的详细 Excel 报告。\n\n当前净值: {stats['broker_value']:.2f}"
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # 添加附件
        with open(filename, "rb") as attachment:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment.read())
        
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename= {filename}")
        msg.attach(part)

        # 发送
        server = smtplib.SMTP_SSL("smtp.qq.com", 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("✅ 邮件发送成功！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

def run_job():
    cerebro = bt.Cerebro()
    cerebro.addstrategy(SimpleStrategy)
    
    # 1. 加载数据
    for symbol in STOCK_POOL:
        df = get_stock_data(symbol)
        if df is not None:
            cerebro.adddata(bt.feeds.PandasData(dataname=df, name=symbol))

    cerebro.broker.setcash(1000000.0)
    cerebro.broker.setcommission(commission=0.0003)
    
    # 2. 添加分析器
    cerebro.addanalyzer(bt.analyzers.PyFolio, _name='pyfolio')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')

    print(f"💰 初始资金: 1,000,000")
    results = cerebro.run()
    strat = results[0]
    
    final_value = cerebro.broker.getvalue()
    print(f"💰 最终净值: {final_value:.2f}")

    # 3. 生成 Excel 报告
    print("📊 正在生成 Excel 报告...")
    os.makedirs('reports', exist_ok=True)
    excel_file = 'reports/Strategy_Report.xlsx'
    
    # 提取数据
    pyfolio_stats = strat.analyzers.getbyname('pyfolio')
    returns, positions, transactions, gross_lev = pyfolio_stats.get_pf_items()
    
    # 创建 Excel Writer
    with pd.ExcelWriter(excel_file) as writer:
        # Sheet 1: 核心指标
        summary_data = {
            '指标名称': ['总收益(%)', '夏普比率', '最大回撤(%)', '最终净值'],
            '数值': [
                f"{strat.analyzers.returns.get_analysis()['rtot']:.2f}",
                f"{strat.analyzers.sharpe.get_analysis()['sharperatio']:.2f}",
                f"{strat.analyzers.drawdown.get_analysis()['max']['drawdown']:.2f}",
                f"{final_value:.2f}"
            ]
        }
        pd.DataFrame(summary_data).to_excel(writer, sheet_name='核心指标', index=False)
        
        # Sheet 2: 每日收益
        returns_df = pd.DataFrame(returns)
        returns_df.columns = ['每日收益']
        returns_df.index = returns_df.index.tz_localize(None) # 去除时区
        returns_df.to_excel(writer, sheet_name='每日收益')
        
        # Sheet 3: 交易记录
        trans_df = pd.DataFrame(transactions)
        trans_df.index = trans_df.index.tz_localize(None)
        trans_df.to_excel(writer, sheet_name='交易明细')

    print(f"✅ Excel 报告已生成: {excel_file}")
    
    # 4. 发送邮件 (如果在本地运行没有配置环境变量，这一步会跳过或报错，但在 GitHub 上会运行)
    if os.getenv("EMAIL_PASS"):
        send_email_report({'broker_value': final_value}, excel_file)

if __name__ == '__main__':
    run_job()