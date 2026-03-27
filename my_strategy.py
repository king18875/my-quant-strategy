import backtrader as bt
import akshare as ak
import pandas as pd
import datetime
import os
import quantstats as qs  # 引入量化统计库

# 确保报告文件夹存在
os.makedirs('reports', exist_ok=True)
os.makedirs('data', exist_ok=True)

# 1. 定义数据获取类 (从 AKShare 下载)
class AkShareData(bt.feeds.PandasData):
    params = (
        ('datetime', '日期'),
        ('open', '开盘'),
        ('high', '最高'),
        ('low', '最低'),
        ('close', '收盘'),
        ('volume', '成交量'),
    )

def get_stock_data(symbol, start_date, end_date):
    print(f"🌐 正在通过 AKShare 下载: {symbol}")
    try:
        # 使用 akshare 获取日线数据
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df.empty:
            print(f"❌ 未获取到数据: {symbol}")
            return None
        
        # 数据清洗：重命名列以匹配 backtrader，并设置索引
        # 注意：akshare 返回的列名可能是中文，需确保对应
        df.rename(columns={
            "日期": "datetime", "开盘": "open", "最高": "high", 
            "最低": "low", "收盘": "close", "成交量": "volume"
        }, inplace=True)
        
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        
        # 转换成交量单位 (akshare 通常是手，backtrader 默认是股，这里简单处理，视具体需求而定)
        # 这里假设直接用数值，因为回测主要看趋势
        df['volume'] = df['volume'] * 100 
        
        print(f"✅ 下载成功: {symbol} (共 {len(df)} 条数据)")
        return df
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        return None

# 2. 定义交易策略 (简单的双均线策略示例)
class MyStrategy(bt.Strategy):
    params = (
        ('short_period', 5),   # 短期均线
        ('long_period', 20),   # 长期均线
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        self.buyprice = None
        self.buycomm = None

        # 初始化均线指标
        self.s_short = bt.indicators.SimpleMovingAverage(
            self.datas[0], period=self.p.short_period)
        self.s_long = bt.indicators.SimpleMovingAverage(
            self.datas[0], period=self.p.long_period)
        
        # 绘图设置
        bt.indicators.ExponentialMovingAverage(self.datas[0], period=25)

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            if order.isbuy():
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
                print(f'✅ 买入执行: 价格 {order.executed.price:.2f}, '
                      f'成本 {order.executed.value:.2f}, '
                      f'手续费 {order.executed.comm:.2f}')
            elif order.issell():
                print(f'✅ 卖出执行: 价格 {order.executed.price:.2f}, '
                      f'成本 {order.executed.value:.2f}, '
                      f'手续费 {order.executed.comm:.2f}')
            self.bar_executed = len(self)

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            print('❌ 订单取消/保证金不足/被拒绝')

        self.order = None

    def next(self):
        if self.order:
            return

        if not self.position:
            # 金叉买入：短期均线上穿长期均线
            if self.s_short > self.s_long:
                print(f'📈 买入信号触发 (Short > Long): {self.dataclose[0]:.2f}')
                self.order = self.buy()
        else:
            # 死叉卖出：短期均线下穿长期均线
            if self.s_short < self.s_long:
                print(f'📉 卖出信号触发 (Short < Long): {self.dataclose[0]:.2f}')
                self.order = self.sell()

# 3. 主程序入口
if __name__ == '__main__':
    # 创建大脑引擎
    cerebro = bt.Cerebro()
    
    # 添加策略
    cerebro.addstrategy(MyStrategy, short_period=5, long_period=20)

    # 设置股票代码和日期
    stock_code = "600519" # 贵州茅台
    start_date = "20230101"
    end_date = "20231231"

    # 获取数据
    df = get_stock_data(stock_code, start_date, end_date)

    if df is not None:
        # 将数据喂给大脑
        data = AkShareData(dataname=df)
        cerebro.adddata(data)

        # 设置资金管理
        cerebro.broker.setcash(100000.0) # 初始资金 10万
        cerebro.broker.setcommission(commission=0.0003) # 手续费万分之三
        cerebro.addsizer(bt.sizers.FixedSize, stake=100) # 每次买 100 股

        print(f'📊 初始资金: ¥{cerebro.broker.getvalue():.2f}')

        # --- 关键修改：添加 PyFolio 分析器 ---
        # 这会自动收集每日收益、持仓等数据供 QuantStats 使用
        cerebro.addanalyzer(bt.analyzers.PyFolio, _name='pyfolio')

        # 运行回测
        results = cerebro.run()
        strat = results[0]

        print(f'💰 最终资金: ¥{cerebro.broker.getvalue():.2f}')

        # --- 关键修改：生成 QuantStats 报告 ---
        try:
            # 从 PyFolio 分析器中提取数据
            portfolio_stats = strat.analyzers.getbyname('pyfolio')
            returns, positions, transactions, gross_lev = portfolio_stats.get_pf_items()
            
            # 将索引转换为 UTC 时间 (QuantStats 要求严格的时间格式)
            returns.index = returns.index.tz_localize('UTC')

            # 生成 HTML 报告 (包含资金曲线、回撤图、月度热力图等)
            output_file = 'reports/strategy_report.html'
            qs.reports.html(returns, title=f'{stock_code} 策略回测报告', output=output_file)
            print(f"✅ 专业图表报告已生成: {output_file}")
            print(f"   请在本地或 GitHub Artifacts 中下载查看该 HTML 文件。")
            
        except Exception as e:
            print(f"❌ 生成图表报告失败: {e}")
            print("   请检查数据是否过少（少于30天）或格式是否正确。")

    else:
        print("⚠️ 因数据获取失败，回测未执行。")