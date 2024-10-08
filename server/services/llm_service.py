import os
from langchain_aws.chat_models.bedrock import ChatBedrock
from dotenv import load_dotenv

load_dotenv()

def invoke_llm(user_text):
    """
    Use LLM (Amazon Bedrock) to analyze user input and determine mood/sentiment.
    Returns one of the 9 moods: Angry, Content, Happy, Delighted, Calm, Sleepy, Sad, Depressed, Excited.
    """
    # Create a structured prompt for mood detection
    mood_prompt = f"""
    You are an AI that analyzes the mood of a user based on their input text. 
    Here are the possible moods: Angry, Content, Happy, Delighted, Calm, Sleepy, Sad, Depressed, Excited.
    Please determine the mood of the following text from one of the 9 moods and return just the mood:
    
    User's text: "{user_text}"
    
    Return the mood:
    """
    print("IT IS WORKING")
    print(os.getenv('AWS_ACCESS_KEY_ID'))
    print(os.getenv("AWS_SECRET_ACCESS_KEY"))
    
    llm = ChatBedrock(
        aws_access_key_id= os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key= os.getenv("AWS_SECRET_ACCESS_KEY"),
        model_id="amazon.titan-text-lite-v1",
        region_name="us-west-2"
    )
    
    # Invoke the model with the structured prompt
    response = llm.invoke(mood_prompt)

    
    # Extract and return the mood from the response
    return response.content.strip()  # Ensures no extra spaces or newlines in the returned mood