# Art Producer review: Time Loop

Integration check: **completed**. Role status: **partial**.

Start with the [Developer handoff](deliverables/art_handoff.md), then inspect the [full model input](model_calls/001/request.json), [unedited model output](model_calls/001/response.txt), and [review notes, test results and retained failed attempt](reviewer_notes.md).

The planner uses the real configured model. Only image generation is mocked. The initial Designer input is the unmodified previously recorded real response, imported as a test precondition with no new model call.

The original Benchmark instruction is unchanged. Both Kenney and OpenGameArt interfaces are enabled; actual mount availability is recorded. No hidden evaluation data was read.

## Inputs and full model I/O

- [Original instruction](instruction.md)
- [Source and Designer provenance](source.json)
- [Recorded real Designer input](designer_input.json)
- [Role request](role_request.json)
- [Actual asset sources](asset_sources.json)
- [Public model configuration](provider_config.json)

- [Call 001: full API request](model_calls/001/request.json)
- [Call 001: full prompt](model_calls/001/prompt.json)
- [Call 001: response.json](model_calls/001/response.json)
- [Call 001: response.txt](model_calls/001/response.txt)
- [Call 001: metadata.json](model_calls/001/metadata.json)

## Outputs

- [Role result](role_result.json)
- [Committed state](state_after.json)
- [Summary and checks](summary.json)
- [Tool audit](tool_audit.jsonl)
- [Mock image inputs and output hashes](image_calls.json)
- [Asset manifest](deliverables/asset_manifest.json)
- [Developer handoff](deliverables/art_handoff.md)

## Image outputs

These images are test placeholders where marked MOCK. They are not generated artwork or visual acceptance evidence.

- [player_sheet](deliverables/assets/sprites/player_sheet.png): 128 x 32; mock=True
- [ghost_sheet](deliverables/assets/sprites/ghost_sheet.png): 128 x 32; mock=True
- [tileset_platform](deliverables/assets/tiles/tileset_platform.png): 256 x 128; mock=True
- [switch_button](deliverables/assets/props/switch_button.png): 64 x 32; mock=True
- [pressure_plate](deliverables/assets/props/pressure_plate.png): 64 x 32; mock=True
- [door_gate](deliverables/assets/props/door_gate.png): 64 x 32; mock=True
- [laser_hazard](deliverables/assets/props/laser_hazard.png): 64 x 32; mock=True
- [enemy_patroller](deliverables/assets/sprites/enemy_patroller.png): 64 x 32; mock=True
- [exit_crystal](deliverables/assets/props/exit_crystal.png): 64 x 32; mock=True
- [hud_panel](deliverables/assets/ui/hud_panel.png): 384 x 128; mock=True
- [ui_controls](deliverables/assets/ui/ui_controls.png): 384 x 128; mock=True
- [title_backdrop](deliverables/assets/ui/title_backdrop.png): 1280 x 720; mock=True
