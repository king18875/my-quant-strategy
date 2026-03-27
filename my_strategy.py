import backtrader as bt
import akshare as ak
import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# ================= 1. 策略参数配置区 =================
# 在这里修改策略参数，无需改动核心逻辑
STRATEGY_PARAMS = {
    'short_period': 10,      # 短期均线周期 (金叉买入)
    'long_period': 30,       # 长期均线周期 (趋势判断)
    'stop_loss_pct': 0.08,   # 个股止损比例 (8%)
    'take_profit_pct': 0.20, # 个股止盈比例 (20%，可选)
    'cash_per_stock': 0.50,  # 单只股票占用资金比例 (50%，即同时持有2只)
}

# 股票池配置 (建议选取不同行业的龙头，避免相关性过高)
STOCK_POOL = [
    "600519", # 贵州茅台 (白酒)
    "300750", # 宁德时代 (新能源)
    "000333", # 美的集团 (家电)
    "601318", # 中国平安 (金融)
    "002415", # 海康威视 (科技)
]

# 大盘风控配置
INDEX_STOCK = "000001"  # 上证指数
INDEX_MA_PERIOD = 20    # 大盘参考均线

# 邮箱配置 (从环境变量读取)
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
# =================================================

# --- 2. 数据获取模块 (支持多股) ---
def get_stock_data(symbol, days=100):
    try:
        end_date = datetime.datetime.now().strftime("%Y%m%d")
        start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y%m%d")
        
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df.empty: return None
        
        df.rename(columns={"日期": "datetime", "开盘": "open", "最高": "high", 
                           "最低": "low", "收盘": "close", "成交量": "volume"}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        # 统一成交量单位
        df['volume'] = df['volume'] * 100 
        return df
    except Exception as e:
        print(f"❌ 获取 {symbol} 数据失败: {e}")
        return None

# --- 3. 大盘风控 ---
def check_market_condition():
    try:
        end_date = datetime.datetime.now().strftime("%Y%m%d")
        start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y%m%d")
        
        df_index = ak.stock_zh_a_hist(symbol=INDEX_STOCK, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df_index.empty: return True # 如果获取失败，默认安全，防止误杀
        
        ma20 = df_index['收盘'].rolling(window=INDEX_MA_PERIOD).mean().iloc[-1]
        current_price = df_index['收盘'].iloc[-1]
        
        # 简单的趋势判断：价格在均线之上
        return current_price > ma20
    except Exception as e:
        print(f"❌ 大盘检查出错: {e}")
        return True

# --- 4. 策略核心逻辑 ---
class MultiStockStrategy(bt.Strategy):
    params = tuple(STRATEGY_PARAMS.items())

    def __init__(self):
        # 为每个数据源初始化指标
        self.crossovers = {}
        self.orders = {} # 记录每个股票的订单状态
        
        for i, data in enumerate(self.datas):
            # 计算均线
            short_ma = bt.indicators.SimpleMovingAverage(data, period=self.p.short_period)
            long_ma = bt.indicators.SimpleMovingAverage(data, period=self.p.long_period)
            # 记录金叉死叉信号
            self.crossovers[data._name] = bt.indicators.CrossOver(short_ma, long_ma)
            self.orders[data._name] = None

    def next(self):
        # 1. 全局风控：检查大盘环境
        is_market_safe = check_market_condition()
        if not is_market_safe:
            print(f"⚠️ 大盘环境恶劣，强制平仓或禁止开新仓！")
            # 如果大盘不好，卖出所有持仓
            for data in self.datas:
                if self.getposition(data).size > 0:
                    self.order = self.close(data=data)
            return

        # 2. 遍历股票池，寻找机会
        for data in self.datas:
            name = data._name
            crossover_signal = self.crossovers[name]
            position = self.getposition(data)
            
            # --- 卖出逻辑 ---
            if position.size > 0:
                # 逻辑 A: 死叉卖出
                if crossover_signal < 0:
                    print(f"📉 {name} 趋势走坏 (死叉)，卖出。价格: {data.close[0]:.2f}")
                    self.order = self.close(data=data)
                
                # 逻辑 B: 止损/止盈 (基于成本价)
                # 注意：Backtrader 的 order 执行是异步的，这里做简化处理
                # 实际止损逻辑通常在 notify_order 中处理更精确，但为了代码简洁，这里用 next 检查
                elif data.close[0] < position.price * (1 - self.p.stop_loss_pct):
                     print(f"🛑 {name} 触发止损 (-{self.p.stop_loss_pct*100}%)，卖出。价格: {data.close[0]:.2f}")
                     self.order = self.close(data=data)
                
                elif data.close[0] > position.price * (1 + self.p.take_profit_pct):
                     print(f"💰 {name} 触发止盈 (+{self.p.take_profit_pct*100}%)，卖出。价格: {data.close[0]:.2f}")
                     self.order = self.close(data=data)

            # --- 买入逻辑 ---
            else:
                # 金叉买入
                if crossover_signal > 0:
                    print(f"📈 {name} 趋势走强 (金叉)，买入。价格: {data.close[0]:.2f}")
                    self.order = self.buy(data=data)

# --- 5. 邮件推送 ---
def send_email_report(portfolio_value, positions_info):
    if not EMAIL_USER:
        print("⚠️ 未配置邮箱，跳过发送。")
        return

    subject = f"📊 量化日报 - {datetime.date.today()}"
    
    # 构建持仓表格 HTML
    table_html = "<table border='1' style='border-collapse: collapse; width: 100%;'><tr><th>代码</th><th>数量</th><th>价格</th><th>市值</th></tr>"
    if not positions_info:
        table_html += "<tr><td colspan='4' align='center'>空仓 (现金为王)</td></tr>"
    else:
        for name, pos, price in positions_info:
            if pos > 0:
                market_val = pos * price
                table_html += f"<tr><td>{name}</td><td>{pos}</td><td>{price:.2f}</td><td>{market_val:.2f}</td></tr>"
    table_html += "</table>"

    body = f"""
    <h2>每日复盘报告</h2>
    <p>当前总净值: <b style='font-size: 1.2em; color: red;'>¥{portfolio_value:,.2f}</b></p>
    <hr>
    <h3>当前持仓:</h3>
    {table_html}
    <br><p>大盘状态: {'✅ 安全' if check_market_condition() else '⚠️ 风险'}</p>
    <br><p>--- 来自 GitHub Actions 自动推送 ---</p>
    """
    
    try:
        msg = MIMEText(body, 'html', 'utf-8')
        msg['From'] = Header("量化机器人", 'utf-8')
        msg['To'] = Header("管理员", 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')

        server = smtplib.SMTP_SSL("smtp.qq.com", 465) 
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        server.quit()
        print("✅ 邮件发送成功！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

# --- 6. 主执行流程 ---
def run_job():
    print(f"\n🚀 --- 开始云端量化交易 ---")
    
    cerebro = bt.Cerebro()
    cerebro.addstrategy(MultiStockStrategy)

    # 1. 加载股票池数据
    valid_data_count = 0
    for symbol in STOCK_POOL:
        df = get_stock_data(symbol)
        if df is not None:
            data = bt.feeds.PandasData(dataname=df, name=symbol)
            cerebro.adddata(data)
            valid_data_count += 1
    
    if valid_data_count == 0:
        print("❌ 没有任何股票数据，程序退出。")
        return

    # 2. 资金管理
    # 初始资金 100万 (模拟)
    cerebro.broker.setcash(1000000.0)
    cerebro.broker.setcommission(commission=0.0003)
    
    # 使用 PercentSizer，根据参数配置仓位
    cerebro.addsizer(bt.sizers.PercentSizer, percents=STRATEGY_PARAMS['cash_per_stock'] * 100)

    # 3. 运行
    print(f"💰 初始资金: ¥{cerebro.broker.getvalue():,.2f}")
    cerebro.run()
    final_value = cerebro.broker.getvalue()
    print(f"💰 最终净值: ¥{final_value:,.2f}")

    # 4. 收集持仓信息用于发邮件
    positions_info = []
    for data in cerebro.datas:
        pos = cerebro.broker.getposition(data)
        if pos.size > 0:
            positions_info.append((data._name, pos.size, data.close[0]))

    # 5. 发送报告
    send_email_report(final_value, positions_info)
    print("--- 任务结束 ---")

if __name__ == '__main__':
    run_job()