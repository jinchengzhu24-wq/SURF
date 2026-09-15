[System.Serializable]
public class LevelDesignPlan
{
    public int minSolutionSteps;
    public int maxSolutionSteps;
    public int minWaterAreas;
    public int maxWaterAreas;
    public int minWallObstacleBlocks;
    public int maxWallObstacleBlocks;
    public int minPushes;
    public int maxPushes;
    public int minReversePulls;
    public int maxReversePulls;
    public string style;
    public string archetype;
    public string targetLayout;
    public string obstacleStyle;
    public string waterStyle;
    public string designNote;
    public string corridorPlacement;
    public int corridorWidth;
    public string corridorOrientation;
    public string corridorRole;
    public string corridorPriority;

    public LevelDesignPlan Copy()
    {
        return new LevelDesignPlan
        {
            minSolutionSteps = minSolutionSteps,
            maxSolutionSteps = maxSolutionSteps,
            minWaterAreas = minWaterAreas,
            maxWaterAreas = maxWaterAreas,
            minWallObstacleBlocks = minWallObstacleBlocks,
            maxWallObstacleBlocks = maxWallObstacleBlocks,
            minPushes = minPushes,
            maxPushes = maxPushes,
            minReversePulls = minReversePulls,
            maxReversePulls = maxReversePulls,
            style = style,
            archetype = archetype,
            targetLayout = targetLayout,
            obstacleStyle = obstacleStyle,
            waterStyle = waterStyle,
            designNote = designNote,
            corridorPlacement = corridorPlacement,
            corridorWidth = corridorWidth,
            corridorOrientation = corridorOrientation,
            corridorRole = corridorRole,
            corridorPriority = corridorPriority
        };
    }
}

[System.Serializable]
public class CorridorValidationResult
{
    public bool requested;
    public bool verified;
    public string placement;
    public int width;
    public string orientation;
    public bool uniquePassage;
    public bool playerCanPass;
    public bool boxPassedThrough;
    public string message;

    public CorridorValidationResult Copy()
    {
        return new CorridorValidationResult
        {
            requested = requested,
            verified = verified,
            placement = placement,
            width = width,
            orientation = orientation,
            uniquePassage = uniquePassage,
            playerCanPass = playerCanPass,
            boxPassedThrough = boxPassedThrough,
            message = message
        };
    }
}
