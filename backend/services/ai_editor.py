import os
from pydantic import BaseModel, Field
from typing import Optional
from database.models import Clip
from sqlalchemy.orm import Session
from services.llm_router import call_llm_structured
from config import settings

class ClipEditAction(BaseModel):
	new_start: Optional[float] = Field(None, description="New start time in seconds if user wants to trim/cut start")
	new_end: Optional[float] = Field(None, description="New end time in seconds if user wants to trim/cut end")
	new_title: Optional[str] = Field(None, description="New title/caption hook for the video if requested")
	new_description: Optional[str] = Field(None, description="New video description if requested")
	reasoning: str = Field(..., description="Brief explanation of changes made based on user prompt")

async def process_clip_ai_edit(clip: Clip, prompt: str, db: Session) -> dict:
	"""
	Process user natural language prompt to modify clip using OmniRoute.
	"""
	system_prompt = (
		"You are an AI Video Editor assistant. Your job is to take user requests and translate them into "
		"precise video modifications for a clip. You will return structured data with updated parameters."
	)

	user_prompt = (
		f"Current Clip Details:\n"
		f"- Start Time: {clip.start}s\n"
		f"- End Time: {clip.end}s\n"
		f"- Current Title: {clip.title}\n"
		f"- Current Description: {clip.description}\n\n"
		f"User Edit Request: {prompt}"
	)

	try:
		response: ClipEditAction = await call_llm_structured(
			response_model=ClipEditAction,
			prompt=user_prompt,
			system=system_prompt
		)

		# Apply updates
		if response.new_start is not None:
			clip.start = response.new_start
		if response.new_end is not None:
			clip.end = response.new_end
		if response.new_title:
			clip.title = response.new_title
		if response.new_description:
			clip.description = response.new_description

		db.commit()
		db.refresh(clip)

		return {
			"id": clip.id,
			"start": clip.start,
			"end": clip.end,
			"title": clip.title,
			"description": clip.description,
			"reasoning": response.reasoning
		}
	except Exception as e:
		raise Exception(f"AI Edit failed: {str(e)}")
