import backtrader as bt
import akshare as ak
import pandas as pd
import datetime
import os
# import quantstats as qs # 暂时注释掉，防止因环境缺失报错，先跑通逻辑
# import smtplib ... (邮件模块暂时保留，但需确保环境有配置)

# ================= 1. 全局配置区 =================
BACKTEST_MODE = False # 在 GitHub Actions 里建议先设为 False 跑每日模拟，或者 True 跑回测

# --- 策略参数 ---
STRATEGY_PARAMS = {
    'base_ma_short': 10,
    'base_ma_long': 30,
    'vol_period': 20,       # 成交量均线周期
    'vol_multiplier': 1.5,  # 放量倍数阈值 (成交量需大于均量的1.5倍)
    'atr_period': 14,
    'stop_loss_atr_mul': 2.5,
}

STOCK_POOL = [
    "600519", "300750", "000333", "601318", "002415",
]

# =================================================

# --- 2. 数据获取模块 ---
def get_stock_data(symbol, start_date, end_date):
    try:
        print(f"📥 正在下载 {symbol} 数据...")
        # 注意：akshare 接口可能需要根据你的版本调整，这里假设是最新的
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df.empty: return None
        
        df.rename(columns={"日期": "datetime", "开盘": "open", "最高": "high", 
                           "最低": "low", "收盘": "close", "成交量": "volume"}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        # 这里的成交量单位通常是手，Backtrader不敏感，只要数值对就行
        return df
    except Exception as e:
        print(f"❌ {symbol} 数据获取失败: {e}")
        return None

# --- 3. 智能仓位管理器 ---
class VolatilityTargetingSizer(bt.Sizer):
    params = (('target_volatility', 0.15), ('lookback_period', 20), ('max_position_pct', 1.0),)

    def _getsizing(self, comminfo, cash, data, isbuy):
        if not isbuy: return 0
        closes = data.close.array
        if len(closes) < self.p.lookback_period: return 0
        
        returns = pd.Series(closes[-self.p.lookback_period:]).pct_change()
        daily_vol = returns.std()
        annualized_vol = daily_vol * (252 ** 0.5)
        if annualized_vol == 0: return 0
        
        target_pct = min(self.p.target_volatility / annualized_vol, self.p.max_position_pct)
        target_cash = cash * target_pct
        return int(target_cash / data.close[0])

# --- 4. 核心策略逻辑 (已修复 OBV 错误) ---
class AdaptiveVolumeStrategy(bt.Strategy):
    params = tuple(STRATEGY_PARAMS.items())

    def __init__(self):
        # --- 1. 趋势指标 ---
        self.ma_short = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.base_ma_short)
        self.ma_long = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.base_ma_long)
        self.crossover = bt.indicators.CrossOver(self.ma_short, self.ma_long)
        
        # --- 2. 波动率指标 (风控) ---
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)
        
        # --- 3. 成交量指标 (修复点：使用成交量均线代替 OBV) ---
        # 计算过去 N 天的平均成交量
        self.vol_sma = bt.indicators.SimpleMovingAverage(self.data.volume, period=self.p.vol_period)

        # --- 4. 状态记录 ---
        self.order = None

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy():
                print(f'✅ 买入: {order.executed.price:.2f}')
            elif order.issell():
                print(f'✅ 卖出: {order.executed.price:.2f}')
            self.order = None

    def next(self):
        if self.order: return

        # --- 动态止损距离 ---
        current_stop_dist = self.p.stop_loss_atr_mul * self.atr[0]
        
        # --- 买入逻辑 ---
        if not self.position:
            # 条件1: 均线金叉
            is_trend_up = self.crossover > 0
            # 条件2: 成交量放量 (当前成交量 > 均量 * 倍数)
            # 注意：volume[0] 是当前K线的成交量
            is_volume_surge = self.data.volume[0] > self.vol_sma[0] * self.p.vol_multiplier
            
            if is_trend_up and is_volume_surge:
                print(f'📈 买入信号 | 价格: {self.data.close[0]:.2f} | 成交量: {self.data.volume[0]}')
                self.order = self.buy()
                # 记录止损价
                self.stop_loss_price = self.data.close[0] - current_stop_dist

        # --- 卖出逻辑 ---
        else:
            # 条件1: 均线死叉
            if self.crossover < 0:
                print(f'📉 卖出信号 (死叉)')
                self.order = self.close()
            # 条件2: 触发止损
            elif self.data.close[0] < self.stop_loss_price:
                print(f'🛑 触发止损 | 现价: {self.data.close[0]:.2f}')
                self.order = self.close()

# --- 5. 主执行流程 ---
def run_job():
    cerebro = bt.Cerebro()
    cerebro.addstrategy(AdaptiveVolumeStrategy)
    
    # 模拟时间范围 (如果是 Actions 运行，建议只跑最近几天测试)
    end_d = datetime.datetime.now().strftime("%Y%m%d")
    start_d = (datetime.datetime.now() - datetime.timedelta(days=100)).strftime("%Y%m%d")

    # 加载数据
    for symbol in STOCK_POOL:
        df = get_stock_data(symbol, start_d, end_d)
        if df is not None:
            cerebro.adddata(bt.feeds.PandasData(dataname=df, name=symbol))

    cerebro.broker.setcash(1000000.0)
    cerebro.broker.setcommission(commission=0.0003)
    
    # 挂载仓位管理
    cerebro.addsizer(VolatilityTargetingSizer)

    print(f"💰 初始资金: ¥{cerebro.broker.getvalue():,.2f}")
    cerebro.run()
    print(f"💰 最终净值: ¥{cerebro.broker.getvalue():,.2f}")

if __name__ == '__main__':
    run_job()