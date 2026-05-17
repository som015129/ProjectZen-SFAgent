import os
import sys
import autogen
from autogen import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager, ConversableAgent
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL =os.getenv("OPENAI_MODEL")

print(OPENAI_API_KEY)
print(OPENAI_MODEL)

llm_config = {"model": OPENAI_MODEL}
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

# === Agent 1: Doctor
doctor_gastro = AssistantAgent(
    name="DoctorGastro",
    system_message="You are a gastroenterologist doctor. your specialist is gastroenterologist. You need to understand the patient's symptoms and suggest medication only for gastroentero related. Don't answer anything outside your specialization.",
    llm_config=llm_config
)
doctor_ortho = AssistantAgent(
    name="DoctorOrtho",
    system_message="You are a medical doctor. your specialist is Orthopedic. You need to understand the patient's symptoms and suggest medication only for bone related. Don't answer anything outside your specialization and refer to the required specialist",
    llm_config=llm_config
)
# === Agent 2: Patient
patient = UserProxyAgent(
    name="Patient",
    system_message="You are a patient describing your symptoms. Communicate naturally, like a person might in a doctor's visit.",
    human_input_mode="ALWAYS", # You can change to "ALWAYS" to interact manually
    #human_input_mode="NEVER", # You can change to "ALWAYS" to interact manually
    code_execution_config={"use_docker": False}
)

# === Agent 3: Auditor ==
auditor = AssistantAgent(
    name="Auditor",
    system_message="""
        You are an independent auditor reviewing the conversation for any of the following:
        - Toxic language
        - Inappropriate adyice
        - Harmful recommendations
        After observing the interaction, summarize if any red flags are found.
    """,
    llm_config=llm_config
)

# === Group Chat ===
group_chat = GroupChat(
    agents=[patient,doctor_gastro,doctor_ortho,auditor],
    messages=[],
    max_round=4
)

manager = GroupChatManager(
    groupchat=group_chat,
    llm_config = llm_config
)

input_message = input("Enter your health problem:")

print(input_message)

patient.initiate_chat(
    manager,
    message = input_message
)