import backtrader as bt
import akshare as ak
import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# ================= 配置区域 =================
# 从环境变量读取敏感信息 (GitHub Actions 会自动注入这些变量)
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

# 交易标的
TARGET_STOCK = "600519" 
INDEX_STOCK = "000001"  # 上证指数

# 策略参数
# 在云端运行时，我们通常获取最近 N 天的数据进行模拟
DAYS_TO_FETCH = 60 
# =================================================

os.makedirs('reports', exist_ok=True)

# --- 1. 数据获取 ---
def get_stock_data(symbol, days):
    print(f"🌐 正在获取数据: {symbol}")
    try:
        # 计算日期范围
        end_date = datetime.datetime.now().strftime("%Y%m%d")
        start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y%m%d")
        
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df.empty: return None
        
        df.rename(columns={"日期": "datetime", "开盘": "open", "最高": "high", 
                           "最低": "low", "收盘": "close", "成交量": "volume"}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df['volume'] = df['volume'] * 100 
        return df
    except Exception as e:
        print(f"❌ 数据获取失败: {e}")
        return None

# --- 2. 大盘风控 ---
def check_market_condition():
    print("🔍 正在检查大盘环境...")
    try:
        # 获取上证指数最近30天
        end_date = datetime.datetime.now().strftime("%Y%m%d")
        start_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y%m%d")
        
        df_index = ak.stock_zh_a_hist(symbol=INDEX_STOCK, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df_index.empty: return False
        
        ma20 = df_index['收盘'].rolling(window=20).mean().iloc[-1]
        current_price = df_index['收盘'].iloc[-1]
        
        print(f"📊 上证指数: {current_price}, MA20: {ma20:.2f}")
        
        return current_price > ma20
    except Exception as e:
        print(f"❌ 大盘检查出错: {e}")
        return False

# --- 3. 策略定义 ---
class MySmartStrategy(bt.Strategy):
    params = (('short_period', 5), ('long_period', 20),)

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        self.s_short = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.p.short_period)
        self.s_long = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.p.long_period)

    def next(self):
        if self.order: return

        is_market_safe = check_market_condition() 
        
        if not self.position:
            if is_market_safe and self.s_short > self.s_long:
                print(f'📈 信号：大盘安全 + 金叉 -> 买入')
                self.order = self.buy()
        else:
            if not is_market_safe or self.s_short < self.s_long:
                print(f'📉 信号：大盘风险 或 死叉 -> 卖出')
                self.order = self.sell()

# --- 4. 邮件推送 ---
def send_email_report(portfolio_value, position_str):
    if not EMAIL_USER or not EMAIL_PASS:
        print("⚠️ 未配置邮箱环境变量，跳过邮件发送。")
        return

    subject = f"📊 量化日报 - {datetime.date.today()}"
    body = f"""
    <h2>每日交易复盘</h2>
    <p>当前净值: <b>¥{portfolio_value:,.2f}</b></p>
    <hr>
    <h3>持仓状态:</h3>
    <p>{position_str}</p>
    <br><p>来自 GitHub Actions 自动推送</p>
    """
    
    try:
        msg = MIMEText(body, 'html', 'utf-8')
        msg['From'] = Header("量化机器人", 'utf-8')
        msg['To'] = Header("管理员", 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')

        # 注意：如果是 QQ 邮箱，服务器地址是 smtp.qq.com，端口 465
        server = smtplib.SMTP_SSL("smtp.qq.com", 465) 
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        server.quit()
        print("✅ 邮件发送成功！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

# --- 5. 主执行逻辑 ---
def run_job():
    print(f"\n🚀 --- 开始执行云端任务: {datetime.datetime.now()} ---")
    
    # 1. 获取数据
    df = get_stock_data(TARGET_STOCK, DAYS_TO_FETCH)
    if df is None: return

    # 2. 初始化回测引擎
    cerebro = bt.Cerebro()
    cerebro.addstrategy(MySmartStrategy)
    data = bt.feeds.PandasData(dataname=df, name=TARGET_STOCK)
    cerebro.adddata(data)
    
    cerebro.broker.setcash(100000.0)
    cerebro.broker.setcommission(commission=0.0003)
    cerebro.addsizer(bt.sizers.FixedSize, stake=100)

    # 3. 运行
    print("🔄 正在模拟交易...")
    cerebro.run()
    
    # 4. 获取结果
    portfolio_value = cerebro.broker.getvalue()
    
    # 获取持仓信息
    position_str = "空仓"
    for data in cerebro.datas:
        pos = cerebro.broker.getposition(data)
        if pos.size > 0:
            position_str = f"持有 {data._name}: {pos.size}股"
    
    print(f"💰 最终净值: ¥{portfolio_value:,.2f}")
    print(f"📦 状态: {position_str}")

    # 5. 发送邮件
    send_email_report(portfolio_value, position_str)
    
    print("--- 任务结束 ---")

if __name__ == '__main__':
    run_job()