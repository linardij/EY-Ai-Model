import asyncio
from dotenv import load_dotenv
from typing import Annotated, List
from typing_extensions import TypedDict
from IPython.display import Image, display
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from tools.tools import PDFPlumberTool
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
import os

# Load environment variables from .env file
load_dotenv()

# Access the OPENAI_API_KEY
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Define the Pydantic model for structured output
class InputData(BaseModel):
    pdf_path: str = Field(description="The path to the PDF file.")
    query: str = Field(description="The user's query or purpose.")
class PageSummary(BaseModel):
    page_number: int = Field(description="The page number of the PDF.")
    heading_sentence: str = Field(description="A single sentence summarizing the main idea of the page.")
    key_points: List[str] = Field(description="Three key points summarizing the content.")
class SearchResult(BaseModel):
    content: str = Field(description="The relevant information extracted from the summaries.")
    claimed_page: int = Field(description="The single page where the information originates.")
class SearchResultList(BaseModel):
    results: List[SearchResult] = Field(description="A list of search results.")
class VerificationResult(BaseModel):
    valid: bool = Field(description="Indicates whether the summary matches the page content.")
    explanation: str = Field(description="Provides the reason for the validity of the match.")

# Create parsers
input_parser = PydanticOutputParser(pydantic_object=InputData)
summary_parser = PydanticOutputParser(pydantic_object=PageSummary)
search_result_parser = PydanticOutputParser(pydantic_object=SearchResult)
search_result_list_parser = PydanticOutputParser(pydantic_object=SearchResultList)
verification_parser = PydanticOutputParser(pydantic_object=VerificationResult)

# Define the state structure
class State(TypedDict):
    messages: Annotated[list, add_messages]  # List of messages exchanged in the session
    pdf_path: str  # Path to the PDF file
    query: str  # User's query
    extracted_pages: List[dict]  # Stores extracted page content and metadata
    summarized_pages: List[PageSummary]  # Stores summaries for each page, now using Pydantic model
    search_results: List[SearchResult]  # Stores search results as Pydantic models
    verified_results: List[VerificationResult]  # Stores verified results as Pydantic models

# Initialize LangGraph
graph_builder = StateGraph(State)

# Initialize the LLM and bind the PDFPlumberTool
llm = ChatOpenAI(model="gpt-4o-mini")
pdf_tool = PDFPlumberTool()
llm_with_tools = llm.bind_tools([pdf_tool])

async def process_input(state: State):
    if not state["messages"]:
        raise ValueError("No input provided.")

    # Extract the user message
    user_message = state["messages"][-1].content

    # Define the parsing prompt
    parsing_prompt = (
        f"You are a helpful assistant. Extract the following details from the user's input:\n\n"
        f"User Input: \"{user_message}\"\n\n"
        f"{input_parser.get_format_instructions()}"
    )

    # Use the LLM to extract information
    response = await llm.ainvoke([{"role": "user", "content": parsing_prompt}])

    # Parse the response
    try:
        parsed_data = input_parser.parse(response.content)
        return {"pdf_path": parsed_data.pdf_path, "query": parsed_data.query}
    except Exception as e:
        print(f"Error parsing input: {e}")
        raise ValueError("Failed to extract PDF path and query.")

def process_pdf(state: State):
    pdf_path = state.get("pdf_path")
    if not pdf_path:
        raise ValueError("PDF path not found.")

    # Use the PDF tool to extract the pages
    pdf_result = pdf_tool.invoke({"pdf_path": pdf_path})
    pages = pdf_result.get("pages", [])

    # Attach both content and page number for processing
    extracted_pages = [
        {"page_number": page["page_number"], "content": page["content"]}
        for page in pages
    ]
    return {"extracted_pages": extracted_pages}

async def summarize_page(state: State):
    async def summarize(page_data):
        """Helper function to invoke LLM asynchronously for summarization."""
        page_number = page_data["page_number"]
        content = page_data["content"]

        # Define the prompt
        prompt = (
            f"You are an advanced document summarizer. Summarize the following content from page {page_number} of a document. "
            "Your summary should have a heading sentence and three key points. Ensure that at least one of the points is qualitative "
            "and one is quantitative. Each point should reflect significant facts or insights and be concise. Below is the content for summarization:\n\n"
            f'"{content}"\n\n'
            "Please respond using the following structure in valid JSON format:\n"
            f"{summary_parser.get_format_instructions()}"
        )

        # Invoke the LLM and parse the response
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        return summary_parser.parse(response.content)

    # Summarize all pages concurrently
    tasks = [summarize(page_data) for page_data in state["extracted_pages"]]
    summaries = await asyncio.gather(*tasks)

    # Prepare summarized pages
    summarized_pages = [
        {
            "page_number": summary.page_number,
            "heading_sentence": summary.heading_sentence,
            "key_points": summary.key_points,
        }
        for summary in summaries
    ]
    return {"summarized_pages": summarized_pages}

async def search_summaries(state: State):
    query = state.get("query")
    summaries = state.get("summarized_pages")

    if not query:
        raise ValueError("Query not found.")
    if not summaries:
        raise ValueError("Summaries not found.")

    # Combine summaries into a text block
    concatenated_summaries = "\n\n".join(
        f"Page {summary['page_number']}:\n"
        f"- **Heading Sentence**: {summary['heading_sentence']}\n"
        f"- **Key Points**:\n"
        f"  1. {summary['key_points'][0]}\n"
        f"  2. {summary['key_points'][1]}\n"
        f"  3. {summary['key_points'][2]}"
        for summary in summaries
    )

    # Define the search prompt
    search_prompt = (
        f"The following are summaries from a document:\n\n"
        f"{concatenated_summaries}\n\n"
        f"Based on the query: \"{query}\", extract the top 10 relevant points from the summary. "
        f"Each point should be associated with exactly one page number as its source. "
        f"Please respond using the following structure in valid JSON format:\n"
        f"{search_result_list_parser.get_format_instructions()}"
    )

    # Use the LLM to perform the search
    response = await llm.ainvoke([{"role": "user", "content": search_prompt}])
    # print("Raw search response:", response.content)

    try:
        # Parse the response using Pydantic
        parsed_results = search_result_list_parser.parse(response.content)
        return {"search_results": parsed_results.results}
    except Exception as e:
        print(f"Error parsing search results: {e}")
        raise ValueError("Failed to parse search results.")

async def verify_results(state: State):
    search_results = state.get("search_results", [])
    extracted_pages = state.get("extracted_pages", [])

    if not search_results:
        raise ValueError("No search results to verify.")
    if not extracted_pages:
        raise ValueError("No extracted pages to verify against.")

    async def verify(result: SearchResult):
        claimed_page = result.claimed_page
        content = result.content

        # Find the matching page in extracted_pages
        matching_page = next(
            (page for page in extracted_pages if page["page_number"] == claimed_page),
            None
        )
        if not matching_page:
            return None  # If no matching page is found, skip verification

        raw_content = matching_page["content"]

        # Define the strict verification prompt
        verification_prompt = (
            f"Does the following summary originate from the content of Page {claimed_page}?\n\n"
            f"Summary:\n{content}\n\n"
            f"Page {claimed_page} Content:\n{raw_content}\n\n"
            f"Check the following:\n"
            f"- Does the numerical data match exactly?\n"
            f"- Are qualitative descriptions consistent and supported by the content?\n"
            f"- Ensure there is no hallucination.\n\n"
            f"Respond with the following structure in valid JSON format:\n"
            f"{verification_parser.get_format_instructions()}"
        )

        # Call the LLM for verification
        response = await llm.ainvoke([{"role": "user", "content": verification_prompt}])

        try:
            # Parse the verification response
            verification_result = verification_parser.parse(response.content)

            if verification_result.valid:
                return {
                    "content": content,
                    "source": f"Page {claimed_page}",
                    "explanation": verification_result.explanation,
                }
        except Exception as e:
            print(f"Error parsing verification result for Page {claimed_page}: {e}")
        return None

    # Verify all search results asynchronously
    tasks = [verify(result) for result in search_results]
    all_verified_points = await asyncio.gather(*tasks)

    # Filter out None values and format the results
    verified_results = [point for point in all_verified_points if point]

    # Present the verified results
    formatted_results = "\n\n".join(
        f"{result['content']} (Source: {result['source']})"
        for result in verified_results
    )

    return {
        "messages": [
            {"role": "assistant", "content": f"Verified Results:\n\n{formatted_results}"}
        ],
        "verified_results": verified_results,
    }


# Build the graph
graph_builder.add_node("process_input", process_input)
graph_builder.add_node("process_pdf", process_pdf)
graph_builder.add_node("summarize_page", summarize_page)
graph_builder.add_node("search_summaries", search_summaries)
graph_builder.add_node("verify_results", verify_results)

graph_builder.add_edge(START, "process_input")
graph_builder.add_edge("process_input", "process_pdf")
graph_builder.add_edge("process_pdf", "summarize_page")
graph_builder.add_edge("summarize_page", "search_summaries")
graph_builder.add_edge("search_summaries", "verify_results")
graph_builder.add_edge("verify_results", END)

# Compile the graph
graph = graph_builder.compile()


# Compile the graph
graph = graph_builder.compile()

# Display the graph structure (optional)
try:
    img = Image(graph.get_graph().draw_mermaid_png())
    display(img)
except Exception:
    pass

# Function to stream updates from the graph asynchronously
# Function to stream updates from the graph asynchronously
async def astream_graph_updates(user_input: str):
    initial_state = {
        "messages": [{"role": "user", "content": user_input}],
        "pdf_path": "",
        "query": "",
        "extracted_pages": [],
        "summarized_pages": [],
    }
    async for event in graph.astream(initial_state):
        for value in event.values():
            if "messages" in value:
                last_message = value["messages"][-1]
                if isinstance(last_message, dict) and "content" in last_message:
                    print("Assistant:", last_message["content"])

# Run the chatbot
if __name__ == "__main__":
    while True:
        try:
            user_input = input("User: ")
            if user_input.lower() in ["quit", "exit", "q", "bye"]:
                print("Goodbye!")
                break

            asyncio.run(astream_graph_updates(user_input))
        except Exception as e:
            print(f"Error: {e}")
            break