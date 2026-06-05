# RootED Architecture Guide

## Current Phase
Content Population & Validation

The platform foundation has been validated. The current priority is NGSS-aligned content creation, validation, and question-bank population.

## Core Rule
RootED follows the NGSS learning progression framework.

RootED does not create its own learning progression map.

The adaptive engine navigates the NGSS framework rather than inventing pathways.

## Adaptive Engine
Rolling-7 is the mastery system.

Rolling-7 progression operates at the objective level only.

Do not modify adaptive_engine.py, Rolling-7 logic, NGSS graph routing, or content architecture without Command Center approval.

## Content Architecture v1.0
Standard → Objective → Identifier → Question

Objectives measure mastery and can block progression.

Identifiers measure misconceptions, subskills, and diagnostic detail.

Questions collect evidence and are tagged to objectives and identifiers.

## Question Standard v1.0
Every question includes:

- standard_id
- objective_id
- difficulty
- question_type
- prompt
- answer_choices
- correct_answer
- explanation
- identifier_tags
- remediation_tags

Difficulty levels:

- Recall
- Application
- Reasoning

## Codex Guardrails
Codex may work on platform shell, UI, navigation, dashboards, and cleanup reports.

Codex may not modify:

- adaptive_engine.py
- Rolling-7 logic
- standard_links graph behavior
- question data
- content model
- NGSS progression rules

without explicit Command Center approval.