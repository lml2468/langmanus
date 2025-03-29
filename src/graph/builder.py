from langgraph.graph import StateGraph, START
from langgraph.checkpoint.memory import MemorySaver
from .types import State
from .nodes import (
    supervisor_node,
    research_node,
    code_node,
    coordinator_node,
    browser_node,
    reporter_node,
    initial_planner_node,
    replanner_node,
    worker_critic_node,
    final_critic_node,
    answer_node,
)


def build_graph():
    """Build and return the agent workflow graph."""
    # use persistent memory to save conversation history
    # TODO: be compatible with SQLite / PostgreSQL
    memory = MemorySaver()

    # build state graph
    builder = StateGraph(State)
    
    # 添加所有节点
    builder.add_node("coordinator", coordinator_node)
    builder.add_node("initial_planner", initial_planner_node)
    builder.add_node("replanner", replanner_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("researcher", research_node)
    builder.add_node("coder", code_node)
    builder.add_node("browser", browser_node)
    builder.add_node("reporter", reporter_node)
    builder.add_node("worker_critic", worker_critic_node)
    builder.add_node("final_critic", final_critic_node)
    builder.add_node("answer", answer_node)
    
    # 定义工作流连接
    # 入口点连接到coordinator
    builder.add_edge(START, "coordinator")
    
    return builder.compile(checkpointer=memory)
