import backtrader as bt
import akshare as ak
import pandas as pd
import datetime
import os
import quantstats as qs # 用于生成专业回测报告
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# ================= 1. 全局配置区 (小白看这里) =================
# 模式开关：
# True = 回测模式 (下载历史数据，生成HTML图表报告)
# False = 每日模拟模式 (下载最新数据，发邮件告诉你买卖了什么)
BACKTEST_MODE = True 

# 回测时间范围 (仅回测模式有效)
BT_START_DATE = "20210101"
BT_END_DATE = "20231231"

# --- 策略参数 (可以在这里调整，进行优化) ---
STRATEGY_PARAMS = {
    'base_ma_short': 10,      # 基础短期均线周期 (趋势)
    'base_ma_long': 30,       # 基础长期均线周期 (趋势)
    'atr_period': 14,         # ATR计算周期 (波动率)
    'vol_lookback': 20,       # 波动率观察周期
    'stop_loss_atr_mul': 2.5, # 动态止损倍数 (ATR的2.5倍，越大越不容易被止损)
    'max_vol_ratio': 2.5,     # 波动率过滤阈值 (超过这个值不开仓，防止高位接盘)
    'target_volatility': 0.15,# 目标年化波动率 (用于仓位管理，0.15代表15%的风险偏好)
}

# 股票池 (建议选取不同行业的龙头)
STOCK_POOL = [
    "600519", # 贵州茅台 (白酒)
    "300750", # 宁德时代 (新能源)
    "000333", # 美的集团 (家电)
    "601318", # 中国平安 (金融)
    "002415", # 海康威视 (科技)
]

# 邮箱配置 (从环境变量读取，保护隐私)
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
# =================================================

# --- 2. 数据获取模块 ---
def get_stock_data(symbol, start_date, end_date):
    """
    使用 AkShare 下载股票历史数据
    """
    try:
        print(f"📥 正在下载 {symbol} 数据...")
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df.empty: return None
        
        # 数据清洗与重命名，适配 Backtrader
        df.rename(columns={"日期": "datetime", "开盘": "open", "最高": "high", 
                           "最低": "low", "收盘": "close", "成交量": "volume"}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df['volume'] = df['volume'] * 100 # 统一单位
        return df
    except Exception as e:
        print(f"❌ {symbol} 数据获取失败: {e}")
        return None

# --- 3. 智能仓位管理器 (Sizer) ---
class VolatilityTargetingSizer(bt.Sizer):
    """
    根据市场波动率自动调整仓位大小。
    市场越动荡，买得越少；市场越平静，买得越多。
    """
    params = (('target_volatility', 0.15), ('lookback_period', 20), ('max_position_pct', 1.0),)

    def _getsizing(self, comminfo, cash, data, isbuy):
        if not isbuy: return 0
        closes = data.close.array
        if len(closes) < self.p.lookback_period: return 0
        
        # 计算最近的年化波动率
        returns = pd.Series(closes[-self.p.lookback_period:]).pct_change()
        daily_vol = returns.std()
        annualized_vol = daily_vol * (252 ** 0.5) # 252是一年的交易日
        if annualized_vol == 0: return 0
        
        # 核心公式：目标仓位 = 目标风险 / 当前风险
        target_pct = min(self.p.target_volatility / annualized_vol, self.p.max_position_pct)
        
        # 计算应该买多少钱的货
        target_cash = cash * target_pct
        return int(target_cash / data.close[0])

# --- 4. 核心策略逻辑 ---
class AdaptiveVolumeStrategy(bt.Strategy):
    """
    自适应量价趋势策略
    结合了：均线趋势 + 成交量验证 + 波动率风控
    """
    params = tuple(STRATEGY_PARAMS.items())

    def __init__(self):
        # --- 1. 趋势指标 (眼睛) ---
        self.ma_short = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.base_ma_short)
        self.ma_long = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.base_ma_long)
        self.crossover = bt.indicators.CrossOver(self.ma_short, self.ma_long) # 金叉/死叉
        
        # --- 2. 波动率指标 (风控) ---
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)
        self.atr_ma = bt.indicators.SimpleMovingAverage(self.atr, period=self.p.vol_lookback)
        
        # --- 3. 成交量指标 (验证) ---
        self.obv = bt.indicators.OnBalanceVolume(self.data) # OBV能量潮
        self.obv_sma = bt.indicators.SimpleMovingAverage(self.obv, period=20)

        # --- 4. 状态记录 ---
        self.order = None
        self.buyprice = None
        self.stop_loss_price = None

    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Completed]:
            if order.isbuy():
                self.buyprice = order.executed.price
                print(f'✅ 买入: {order.executed.price:.2f} | OBV: {self.obv[0]:.0f}')
            elif order.issell():
                print(f'✅ 卖出: {order.executed.price:.2f}')
            self.order = None

    def next(self):
        """主循环：每个时间点执行一次"""
        if self.order: return

        # --- 第一步：计算动态风控参数 ---
        # 计算当前波动率是历史均值的多少倍
        vol_ratio = self.atr[0] / self.atr_ma[0]
        # 动态止损距离 = ATR * 倍数 (波动越大，止损越宽)
        current_stop_dist = self.p.stop_loss_atr_mul * self.atr[0]
        
        # --- 第二步：判断买入条件 ---
        if not self.position:
            # 条件1: 均线金叉 (趋势向上)
            is_trend_up = self.crossover > 0
            # 条件2: OBV > OBV均线 (有资金流入，拒绝假突破)
            is_volume_confirmed = self.obv[0] > self.obv_sma[0]
            # 条件3: 波动率不过高 (不接飞刀，不买在情绪最亢奋时)
            is_market_calm = vol_ratio < self.p.max_vol_ratio
            
            # 只有三个条件都满足才买入
            if is_trend_up and is_volume_confirmed and is_market_calm:
                print(f'📈 买入信号 | 价格: {self.data.close[0]:.2f} | 波动率: {vol_ratio:.2f}')
                self.order = self.buy()
                # 设定初始止损价
                self.stop_loss_price = self.data.close[0] - current_stop_dist

        # --- 第三步：判断卖出条件 ---
        else:
            # 条件1: 均线死叉 (趋势坏了)
            if self.crossover < 0:
                print(f'📉 卖出信号 (死叉)')
                self.order = self.close()
            # 条件2: 触发动态止损 (保命)
            elif self.data.close[0] < self.stop_loss_price:
                print(f'🛑 触发动态止损 | 止损价: {self.stop_loss_price:.2f} | 现价: {self.data.close[0]:.2f}')
                self.order = self.close()

# --- 5. 邮件推送功能 ---
def send_email_report(portfolio_value, positions_info):
    if not EMAIL_USER: return
    subject = f"📊 量化日报 - {datetime.date.today()}"
    
    # 生成持仓表格
    table_html = "<table border='1' style='border-collapse: collapse; width: 100%;'><tr><th>代码</th><th>数量</th><th>价格</th><th>市值</th></tr>"
    if not positions_info:
        table_html += "<tr><td colspan='4' align='center'>空仓 (现金为王)</td></tr>"
    else:
        for name, pos, price in positions_info:
            if pos > 0: table_html += f"<tr><td>{name}</td><td>{pos}</td><td>{price:.2f}</td><td>{pos*price:.2f}</td></tr>"
    table_html += "</table>"
    
    body = f"""<h2>每日复盘报告</h2><p>总净值: <b>¥{portfolio_value:,.2f}</b></p><hr><h3>持仓:</h3>{table_html}<br><p>来自 GitHub Actions</p>"""
    
    try:
        msg = MIMEText(body, 'html', 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        server = smtplib.SMTP_SSL("smtp.qq.com", 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        server.quit()
    except Exception as e: print(f"❌ 邮件失败: {e}")

# --- 6. 主执行流程 ---
def run_job():
    cerebro = bt.Cerebro()
    cerebro.addstrategy(AdaptiveVolumeStrategy)
    
    # 确定时间范围
    start_d = BT_START_DATE if BACKTEST_MODE else (datetime.datetime.now() - datetime.timedelta(days=100)).strftime("%Y%m%d")
    end_d = BT_END_DATE if BACKTEST_MODE else datetime.datetime.now().strftime("%Y%m%d")

    # 加载股票池数据
    for symbol in STOCK_POOL:
        df = get_stock_data(symbol, start_d, end_d)
        if df is not None:
            cerebro.adddata(bt.feeds.PandasData(dataname=df, name=symbol))

    # 设置初始资金
    cerebro.broker.setcash(1000000.0)
    cerebro.broker.setcommission(commission=0.0003)
    
    # 挂载智能仓位管理器
    cerebro.addsizer(VolatilityTargetingSizer, target_volatility=STRATEGY_PARAMS['target_volatility'])

    print(f"💰 初始资金: ¥{cerebro.broker.getvalue():,.2f}")
    
    # 运行回测或模拟
    if BACKTEST_MODE:
        # 添加分析器
        cerebro.addanalyzer(bt.analyzers.PyFolio, _name='pyfolio')
        results = cerebro.run()
        
        # 生成专业报告
        strat = results[0]
        try:
            portfolio_stats = strat.analyzers.getbyname('pyfolio')
            returns, positions, transactions, gross_lev = portfolio_stats.get_pf_items()
            returns.index = returns.index.tz_localize('UTC')
            # 生成 HTML 报告
            qs.reports.html(returns, title="自适应量价策略回测", output='reports/backtest_report.html')
            print("✅ 回测报告已生成: reports/backtest_report.html")
        except Exception as e: print(f"❌ 报告生成失败: {e}")
    else:
        # 每日模拟运行
        cerebro.run()
        # 收集持仓信息发邮件
        positions_info = [(d._name, cerebro.broker.getposition(d).size, d.close[0]) for d in cerebro.datas]
        send_email_report(cerebro.broker.getvalue(), positions_info)

    print(f"💰 最终净值: ¥{cerebro.broker.getvalue():,.2f}")

if __name__ == '__main__':
    run_job()