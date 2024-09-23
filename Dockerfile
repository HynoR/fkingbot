# 使用官方 Python 基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 复制当前目录所有文件到工作目录
COPY . /app

# 安装必要的软件包
RUN pip install --no-cache-dir -r requirements.txt

# 暴露 Flask 应用的端口
EXPOSE 5000

# 设置环境变量
ENV API_TOKEN=YOUR_TELEGRAM_BOT_API_TOKEN
ENV ADMIN_KEY=YOUR_ADMIN_KEY
ENV BASE_URL=https://test.org/user/tgauth?key=

# 启动应用
CMD ["python", "app.py"]
