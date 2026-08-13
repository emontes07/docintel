# This file contains system messages and prompts for various tasks in the application.

# TODO: replace with the document-intelligence extraction prompt. It must describe
# the fields the model should populate and match the schema passed to
# LLMClient.complete_structured().
extraction_system_message = """You are an expert at extracting structured information from documents.
Analyze the provided artifact and return the requested fields.
Return only fields defined by the supplied schema, and leave a field empty if the
artifact does not support a confident answer.
"""
