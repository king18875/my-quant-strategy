# utils/html_report.py
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd

def generate_html_report(report):
    nav_df = report['nav_df']
    
    fig_nav = go.Figure()
    fig_nav.add_trace(go.Scatter(x=nav_df['date'], y=nav_df['nav'], mode='lines', name='净值'))
    fig_nav.update_layout(title='📈 模拟账户净值曲线', xaxis_title='日期', yaxis_title='净值 (¥)', template='plotly_white')

    if report['positions']:
        labels = list(report['positions'].keys())
        values = [v['value'] for v in report['positions'].values()]
        fig_pie = px.pie(names=labels, values=values, title='当前持仓分布')
    else:
        fig_pie = go.Figure()
        fig_pie.add_annotation(text="无持仓", x=0.5, y=0.5, showarrow=False)

    nav_html = fig_nav.to_html(full_html=False, include_plotlyjs='cdn')
    pie_html = fig_pie.to_html(full_html=False, include_plotlyjs='cdn')

    html_content = f"""
    <html>
    <head><meta charset="utf-8"><title>量化日报</title></head>
    <body style="font-family: Arial, sans-serif; max-width: 1000px; margin: auto; padding: 20px;">
        <h2>📊 个人量化模拟交易日报 - {pd.Timestamp.now().date()}</h2>
        <p><strong>初始资金：</strong>¥{report['initial_value']:,.2f}</p>
        <p><strong>当前净值：</strong>¥{report['final_value']:,.2f}</p>
        <p><strong>总收益率：</strong>{report['total_return_pct']:.2f}%</p>
        <p><strong>最大回撤：</strong>{report['max_drawdown_pct']:.2f}%</p>
        <p><strong>风控状态：</strong>{"⚠️ 已触发清仓" if report['drawdown_triggered'] else "🟢 正常"}</p>
        
        <h3>📈 净值曲线</h3>
        {nav_html}
        
        <h3>🥧 持仓分布</h3>
        {pie_html}
        
        <hr>
        <small>策略说明：四因子选股（RSI超卖、MACD金叉、低PE、放量确认）<br>
        严格遵守 A 股 T+1 规则 | 单股≤30% | 总仓位≤90%</small>
    </body>
    </html>
    """

    text_summary = f"净值: ¥{report['final_value']:,.2f} | 收益率: {report['total_return_pct']:.2f}%"
    return html_content, text_summary
