# 金融新闻行情分析 Agent

一个基于智谱大模型的金融新闻行情分析系统，能够自动获取沪深300指数数据、抓取财经快讯，并利用 AI 进行新闻情感分析。

## 项目结构

```
finance-news-agent/
├── data_fetcher.py          # 沪深300指数历史数据获取
├── news_fetcher.py          # 财经快讯抓取（东方财富/新浪）
├── sentiment_analyzer.py    # 新闻情感分析（智谱 GLM 大模型）
├── requirements.txt         # Python 依赖清单
├── .env                     # 环境变量配置（API Key，不提交到 Git）
├── .env.example             # 环境变量配置模板
└── .gitignore               # Git 忽略规则
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key（重要！）

运行前需要配置智谱大模型的 API Key：

1. 在项目根目录找到 `.env` 文件
2. 在 `ZHIPU_API_KEY=` 后面填入你的 API Key：

```env
ZHIPU_API_KEY=your_api_key_here
```

3. API Key 获取地址：https://open.bigmodel.cn/usercenter/apikeys

> **注意**：`.env` 文件包含敏感信息，已被 `.gitignore` 排除，不会提交到仓库。

### 3. 运行程序

```bash
# 获取沪深300指数数据
python data_fetcher.py

# 抓取财经快讯
python news_fetcher.py

# 运行情感分析
python sentiment_analyzer.py
```

## 环境变量说明

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `ZHIPU_API_KEY` | 智谱大模型 API Key | 是 |

## 技术栈

- **akshare** - 金融数据获取（沪深300指数、财经快讯）
- **zhipuai** - 智谱大模型 SDK（情感分析）
- **python-dotenv** - 环境变量管理
