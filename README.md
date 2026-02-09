# SQL 智能教学系统

基于 SQL 练习 + AI 辅导的在线教学项目：学生做题、自动判题、多轮对话答疑；教师管理题目、按知识点 AI 出题；支持多语言（简体/英文/繁体）。

---

## 功能概览

- **学生端**：选题、写 SQL、提交判题、查看表结构预览、与 AI 教师对话、限时挑战、难度反馈、多语言切换
- **教师端**：题目 CRUD、按知识点 AI 生成题目、补全题目多语言、知识点多语言展示
- **后端**：JWT 认证、题目与提交管理、SQL 安全执行与结果对比、AI 提示与对话、经验与等级

---

## 技术栈

| 端 | 技术 |
|----|------|
| 后端 | Python 3.10+、FastAPI、SQLAlchemy（async）、MySQL、Alembic、JWT、FastAPI-Mail、OpenAI 兼容 API |
| 前端 | Vue 3、uni-app、TypeScript、Vite |

---

## 项目结构

```
web_project/
├── sql-edu-backend/     # 后端 FastAPI
│   ├── main.py          # 应用入口
│   ├── routers/         # 路由：auth, ai, question
│   ├── core/            # 业务：判题、AI、难度、经验、知识点等
│   ├── models/          # 数据库模型
│   ├── repository/       # 数据访问
│   ├── schemas/         # Pydantic 请求/响应
│   ├── alembic/         # 数据库迁移
│   └── requirements.txt
├── sql-edu-frontend/    # 前端 uni-app
│   ├── src/
│   │   ├── pages/       # 登录、学生练习、教师端
│   │   ├── api/         # 接口封装
│   │   └── utils/       # request、auth
│   └── package.json
└── docs/                # 文档
    ├── 01-各板块功能介绍.md
    ├── 02-从0实现逻辑链.md
    ├── 03-前后端功能测试文档.md
    ├── 04-从零到运行-环境与安装步骤.md
    
```

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- Node.js 18+
- MySQL（后端主库）

### 2. 后端

```bash
cd sql-edu-backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

在项目根目录或 `sql-edu-backend` 下新建 `.env`，配置例如：

- `DB_URL`：MySQL 连接串（如 `mysql+aiomysql://user:pass@host/dbname`）
- `JWT_SECRET_KEY`、`SECRET_KEY`
- 邮件相关：`MAIL_USERNAME`、`MAIL_PASSWORD`、`MAIL_FROM` 等
- AI：`AI_API_KEY`、`AI_BASE_URL`（可选 `AI_MODEL_NAME`）

执行迁移并启动：

```bash
alembic upgrade head
uvicorn main:app --reload
```

默认 http://127.0.0.1:8000

### 3. 前端

```bash
cd sql-edu-frontend
npm install
npm run dev:h5
```

浏览器打开控制台输出的地址（如 http://localhost:5173）。在 `src/utils/request.ts` 中确认 `baseURL` 指向后端（如 `http://127.0.0.1:8000`）。

---

## 文档

| 文档 | 说明 |
|------|------|
| [04-从零到运行-环境与安装步骤](docs/04-从零到运行-环境与安装步骤.md) | **从环境配置到前后端启动**的一步步操作（先装什么、再装什么、怎么配 .env、怎么跑） |
| [01-各板块功能介绍](docs/01-各板块功能介绍.md) | 前后端各模块/路由功能说明 |
| [02-从0实现逻辑链](docs/02-从0实现逻辑链.md) | 从数据库到前端的实现顺序与数据流 |
| [03-前后端功能测试文档](docs/03-前后端功能测试文档.md) | 接口与页面测试步骤、预期与 pytest 说明 |
| [05-项目目录结构说明](docs/05-项目目录结构说明.md) | 项目完整文件目录结构说明 |

---

## 测试（后端）

```bash
cd sql-edu-backend
pytest tests/ -v
```

---

