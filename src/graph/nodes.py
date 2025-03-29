import logging
import json
import logging
from copy import deepcopy
from typing import Literal
from langchain_core.messages import HumanMessage, BaseMessage, AIMessage

from langchain_core.tools import tool
from langgraph.types import Command

from src.agents import research_agent, coder_agent, browser_agent
from src.llms.llm import get_llm_by_type
from src.config import TEAM_MEMBERS
from src.config.agents import AGENT_LLM_MAP
from src.prompts.template import apply_prompt_template
from src.tools.search import tavily_tool
from src.utils.json_utils import repair_json_output
from .types import State, Router

logger = logging.getLogger(__name__)

RESPONSE_FORMAT = "Response from {}:\n\n<response>\n{}\n</response>\n\n*Please execute the next step.*"


@tool
def handoff_to_planner():
    """Handoff to planner agent to do plan."""
    # This tool is not returning anything: we're just using it
    # as a way for LLM to signal that it needs to hand off to planner agent
    return

def answer_node(state: State) -> Command[Literal["__end__"]]:
    """Node for generating a final comprehensive answer to the user based on all completed tasks."""
    logger.info("Answer node generating final response to user")
    messages = apply_prompt_template("answer", state)
    response = get_llm_by_type(AGENT_LLM_MAP.get("answer", "reasoning")).invoke(messages)
    logger.debug(f"Answer node response: {response.content}")
    
    return Command(
        update={
            "messages": [
                AIMessage(
                    content=response.content,
                    name="answer",
                )
            ]
        },
        goto="__end__",
    )
    

def research_node(state: State) -> Command[Literal["worker_critic"]]:
    """Node for the researcher agent that performs research tasks."""
    logger.info("Research agent starting task")
    result = research_agent.invoke(state)
    logger.info("Research agent completed task")
    response_content = result["messages"][-1].content
    # 尝试修复可能的JSON输出
    response_content = repair_json_output(response_content)
    logger.debug(f"Research agent response: {response_content}")
    return Command(
        update={
            "messages": [
                HumanMessage(
                    content=response_content,
                    name="researcher",
                )
            ]
        },
        goto="worker_critic",
    )


def code_node(state: State) -> Command[Literal["worker_critic"]]:
    """Node for the coder agent that executes Python code."""
    logger.info("Code agent starting task")
    result = coder_agent.invoke(state)
    logger.info("Code agent completed task")
    response_content = result["messages"][-1].content
    # 尝试修复可能的JSON输出
    response_content = repair_json_output(response_content)
    logger.debug(f"Code agent response: {response_content}")
    return Command(
        update={
            "messages": [
                HumanMessage(
                    content=response_content,
                    name="coder",
                )
            ]
        },
        goto="worker_critic",
    )


def browser_node(state: State) -> Command[Literal["worker_critic"]]:
    """Node for the browser agent that performs web browsing tasks."""
    logger.info("Browser agent starting task")
    result = browser_agent.invoke(state)
    logger.info("Browser agent completed task")
    response_content = result["messages"][-1].content
    # 尝试修复可能的JSON输出
    response_content = repair_json_output(response_content)
    logger.debug(f"Browser agent response: {response_content}")
    return Command(
        update={
            "messages": [
                HumanMessage(
                    content=response_content,
                    name="browser",
                )
            ]
        },
        goto="worker_critic",
    )


def supervisor_node(state: State) -> Command[Literal[*TEAM_MEMBERS, "worker_critic"]]:
    """Supervisor node that executes a single task by delegating to the appropriate worker."""
    
    # 获取任务列表
    todo_tasks = state.get("todo_tasks", [])
    current_task = state.get("current_task", {})
    
    # 如果当前没有任务，但todo_tasks中有任务，则取第一个任务
    if not current_task and todo_tasks:
        current_task = todo_tasks[0]
        todo_tasks = todo_tasks[1:]
        logger.info(f"Supervisor selecting first task from todo list: {current_task.get('id', 'unknown')} - {current_task.get('description', 'unknown')}")
    elif not current_task:
        logger.warning("Supervisor received no tasks to execute, returning to replanner")
        return Command(goto="replanner")
    else:
        logger.info(f"Supervisor executing current task: {current_task.get('id', 'unknown')} - {current_task.get('description', 'unknown')}")
    
    # 根据当前任务决定要调用的worker
    worker = current_task.get("agent_name")
    
    if worker not in TEAM_MEMBERS:
        logger.warning(f"Invalid worker '{worker}' in task, returning to replanner")
        return Command(goto="replanner")
    
    logger.info(f"Supervisor delegating task to: {worker}")

    return Command(
        goto=worker,
        update={
            "todo_tasks": todo_tasks,
            "current_task": current_task,
        }
    )


def initial_planner_node(state: State) -> Command[Literal["supervisor"]]:
    """Initial planner node that generates the first task plan."""
    logger.info("Initial planner generating task plan")
    
    # 获取当前状态
    todo_tasks = []
    completed_tasks = []
    
    # 生成新的任务列表
    messages = apply_prompt_template("planner", state)
    # whether to enable deep thinking mode
    llm = get_llm_by_type("basic")
    if state.get("deep_thinking_mode"):
        llm = get_llm_by_type("reasoning")
    if state.get("search_before_planning"):
        searched_content = tavily_tool.invoke({"query": state["messages"][-1].content})
        if isinstance(searched_content, list):
            messages = deepcopy(messages)
            messages[
                -1
            ].content += f"\n\n# Relative Search Results\n\n{json.dumps([{'title': elem['title'], 'content': elem['content']} for elem in searched_content], ensure_ascii=False)}"
        else:
            logger.error(
                f"Tavily search returned malformed response: {searched_content}"
            )
    stream = llm.stream(messages)
    full_response = ""
    for chunk in stream:
        full_response += chunk.content
    logger.debug(f"Current state messages: {state['messages']}")
    logger.debug(f"Initial planner response: {full_response}")

    # 解析计划中的任务列表
    try:
        full_response = repair_json_output(full_response)
        # 尝试从响应中提取任务列表
        try:
            plan_data = json.loads(full_response)
            if isinstance(plan_data, dict) and "steps" in plan_data:
                tasks = plan_data["steps"]
                for task in tasks:
                    if isinstance(task, dict) and "agent_name" in task and "description" in task:
                        # 确保worker是有效的团队成员
                        if task["agent_name"] in TEAM_MEMBERS:
                            todo_tasks.append(task)
                        else:
                            logger.warning(f"Invalid worker in task: {task}")
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Failed to parse tasks from plan: {e}")
    except json.JSONDecodeError:
        logger.warning("Planner response is not a valid JSON")

    # 如果没有成功解析出任务，创建一个默认任务
    if not todo_tasks:
        logger.warning("No valid tasks found in plan, creating default task")
        todo_tasks = [{
            "agent_name": "researcher",
            "description": "Research the user's query and provide relevant information"
        }]
        
    # 记录完整计划
    state["full_plan"] = full_response
    state["messages"].append(HumanMessage(content=full_response, name="initial_planner"))
    
    # 确保所有任务都有ID
    for i, task in enumerate(todo_tasks):
        if "id" not in task:
            task["id"] = str(i + 1)
    
    logger.info(f"Initial plan created with {len(todo_tasks)} tasks")
    return Command(
        update={
            "todo_tasks": todo_tasks,
            "completed_tasks": completed_tasks,
            "current_task": {},  # 留空，由supervisor选择任务
            "task_result": {}
        },
        goto="supervisor",
    )


def replanner_node(state: State) -> Command[Literal["supervisor", "final_critic"]]:
    """Replanner node that handles task results and decides next steps."""
    logger.info("Replanner evaluating task results and next steps")
    
    # 获取当前状态
    todo_tasks = state.get("todo_tasks", [])
    completed_tasks = state.get("completed_tasks", [])
    current_task = state.get("current_task", {})
    task_result = state.get("task_result", {})
    
    # 处理任务评估结果
    need_replan = False
    feedback = ""
    if task_result:
        feedback = task_result.get("feedback", "")
        if "REPLAN" in feedback:
            logger.info("Worker critic suggested replanning")
            need_replan = True
            # 清空当前任务列表，准备重新规划
            todo_tasks = []
    
    # 如果需要重新规划，生成新的任务列表
    if need_replan:
        logger.info("Regenerating task plan based on feedback")
        
        # 将worker_critic的反馈添加到状态中，以便在重新规划时参考
        state["critic_feedback"] = feedback
        
        messages = apply_prompt_template("replanner", state)
        llm = get_llm_by_type("reasoning")  # 使用更强的推理能力进行重新规划
        
        stream = llm.stream(messages)
        full_response = ""
        for chunk in stream:
            full_response += chunk.content
        logger.debug(f"Replanner response: {full_response}")

        # 解析重新规划的任务列表
        try:
            full_response = repair_json_output(full_response)
            try:
                plan_data = json.loads(full_response)
                if isinstance(plan_data, dict) and "steps" in plan_data:
                    tasks = plan_data["steps"]
                    for task in tasks:
                        if isinstance(task, dict) and "agent_name" in task and "description" in task:
                            if task["agent_name"] in TEAM_MEMBERS:
                                todo_tasks.append(task)
                            else:
                                logger.warning(f"Invalid worker in task: {task}")
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"Failed to parse tasks from replan: {e}")
        except json.JSONDecodeError:
            logger.warning("Replanner response is not a valid JSON")

        # 如果重新规划失败，创建一个默认任务
        if not todo_tasks:
            logger.warning("No valid tasks found in replan, creating default task")
            todo_tasks = [{
                "agent_name": "researcher",
                "description": "Research the user's query with more details based on previous feedback"
            }]
            
        # 记录重新规划
        state["full_plan"] = full_response
        state["messages"].append(HumanMessage(content=full_response, name="replanner"))
    
    # 确保所有任务都有唯一ID
    for i, task in enumerate(todo_tasks):
        if "id" not in task:
            task["id"] = str(len(completed_tasks) + i + 1)
    
    # 决定下一步操作
    if todo_tasks:
        logger.info(f"Sending {len(todo_tasks)} tasks to supervisor")
        goto = "supervisor"
    else:
        # 所有任务已完成，转到最终评估
        logger.info("All tasks completed, moving to final evaluation")
        goto = "final_critic"
    
    return Command(
        update={
            "todo_tasks": todo_tasks,
            "completed_tasks": completed_tasks,
            "current_task": {},  # 留空，由supervisor选择任务
            "task_result": {}
        },
        goto=goto,
    )


def coordinator_node(state: State) -> Command[Literal["initial_planner", "__end__"]]:
    """Coordinator node that communicate with customers."""
    logger.info("Coordinator talking.")
    messages = apply_prompt_template("coordinator", state)
    response = (
        get_llm_by_type(AGENT_LLM_MAP["coordinator"])
        .bind_tools([handoff_to_planner])
        .invoke(messages)
    )
    logger.debug(f"Current state messages: {state['messages']}")

    goto = "__end__"
    
    # 检查是否调用了工具
    if len(response.tool_calls) > 0:
        goto = "initial_planner"
        return Command(goto=goto)
    
    return Command(
        goto=goto,
        update={
            "messages": [
                AIMessage(
                    content=response.content,
                    name="coordinator",
                )
            ]
        },
    )


def worker_critic_node(state: State) -> Command[Literal["replanner"]]:
    """Worker critic node that evaluates if a specific task has been successfully completed."""
    logger.info("Worker critic evaluating task completion")
    messages = apply_prompt_template("worker_critic", state)
    response = get_llm_by_type(AGENT_LLM_MAP.get("worker_critic", "reasoning")).invoke(messages)
    logger.debug(f"Current state messages: {state['messages']}")
    logger.debug(f"Worker critic response: {response.content}")
    
    # 获取当前状态
    current_task = state.get("current_task", {})
    todo_tasks = state.get("todo_tasks", [])
    completed_tasks = state.get("completed_tasks", [])
    
    # 检查任务是否成功完成
    task_successful = "SUCCESS" in response.content
    task_id = current_task.get("id")
    
    if task_successful:
        logger.info(f"Task '{current_task.get('description', 'unknown')}' successfully completed")
        # 将任务从待办移到已完成
        for i, task in enumerate(todo_tasks):
            if task.get("id") == task_id:
                completed_task = todo_tasks.pop(i)
                completed_tasks.append(completed_task)
                break
    else:
        logger.info(f"Task '{current_task.get('description', 'unknown')}' failed")
        # 对于失败的任务，我们保留在todo_tasks中，由replanner决定是重试还是重新规划
    
    # 创建任务结果对象，包含详细的反馈
    task_result = {
        "task_id": task_id,
        "success": task_successful,
        "feedback": response.content,
        "task_description": current_task.get('description', ''),
        "agent_name": current_task.get('agent_name', '')
    }
    
    # 将评估结果返回给replanner
    return Command(
        update={
            "messages": [
                HumanMessage(
                    content=response.content,
                    name="worker_critic",
                )
            ],
            "todo_tasks": todo_tasks,
            "completed_tasks": completed_tasks,
            "task_result": task_result
        },
        goto="replanner",
    )


def final_critic_node(state: State) -> Command[Literal["replanner", "answer"]]:
    """Final critic node that evaluates if the user's problem has been truly solved."""
    logger.info("Final critic evaluating solution completeness")
    messages = apply_prompt_template("final_critic", state)
    response = get_llm_by_type(AGENT_LLM_MAP.get("final_critic", "reasoning")).invoke(messages)
    logger.debug(f"Current state messages: {state['messages']}")
    logger.debug(f"Final critic response: {response.content}")
    
    # 检查是否需要重新规划
    needs_replanning = "REPLAN" in response.content
    
    goto = "replanner" if needs_replanning else "answer"
    
    if needs_replanning:
        logger.info("Problem not fully solved, returning to replanner for replanning")
        # 重置任务列表，准备重新规划
        return Command(
            update={
                "messages": [
                    HumanMessage(
                        content=response.content,
                        name="final_critic",
                    )
                ],
                "todo_tasks": [],
                "completed_tasks": [],
                "current_task": {},
                "task_result": {}
            },
            goto=goto,
        )
    else:
        logger.info("Problem successfully solved, redirecting to answer node for final answer")
        return Command(
            update={
                "messages": [
                    HumanMessage(
                        content=response.content,
                        name="final_critic",
                    )
                ]
            },
            goto=goto,
        )


def reporter_node(state: State) -> Command[Literal["supervisor"]]:
    """Reporter node that write a final report."""
    logger.info("Reporter write final report")
    messages = apply_prompt_template("reporter", state)
    response = get_llm_by_type(AGENT_LLM_MAP["reporter"]).invoke(messages)
    logger.debug(f"Current state messages: {state['messages']}")
    response_content = response.content
    # 尝试修复可能的JSON输出
    response_content = repair_json_output(response_content)
    logger.debug(f"reporter response: {response_content}")

    return Command(
        update={
            "messages": [
                HumanMessage(
                    content=response_content,
                    name="reporter",
                )
            ]
        },
        goto="worker_critic",
    )
