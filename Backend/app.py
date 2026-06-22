from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/generate-level-plan")
def generate_level_plan():
    return {
        "minSolutionSteps": 16,
        "maxSolutionSteps": 30,
        "minWaterAreas": 1,
        "maxWaterAreas": 2,
        "minWallObstacleBlocks": 2,
        "maxWallObstacleBlocks": 3,
        "style": "medium complexity"
    }