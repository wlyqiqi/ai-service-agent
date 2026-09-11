import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

load_dotenv()

model = ChatOpenAI(
    model=os.getenv("ARK_MODEL", "deepseek-v4-flash"),
    api_key=os.getenv("ARK_API_KEY"),
    base_url=os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3"),
)
app = create_react_agent(model, tools=[])
