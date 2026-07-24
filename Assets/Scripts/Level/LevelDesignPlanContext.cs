using UnityEngine;
using UnityEngine.SceneManagement;

public static class LevelDesignPlanContext
{
    private const string PlanPrefsKey = "SokobanLastLevelDesignPlan";

    private static bool loaded;

    public static LevelDesignPlan Plan { get; private set; }
    public static string ExpandedIdeaText { get; private set; }
    public static string SceneName { get; private set; }
    public static string LatestAdjustmentText { get; private set; }
    public static CorridorValidationResult CorridorValidation { get; private set; }

    public static bool HasPlan
    {
        get
        {
            EnsureLoaded();
            return Plan != null;
        }
    }

    public static void SaveAppliedPlan(
        LevelDesignPlan plan,
        CorridorValidationResult corridorValidation = null)
    {
        if (plan == null)
        {
            Clear();
            return;
        }

        if (plan.corridorPriority == "required"
            && plan.corridorPlacement != "none"
            && (corridorValidation == null || !corridorValidation.verified))
        {
            Debug.LogWarning("LevelDesignPlanContext refused to save an unverified required corridor.");
            Clear();
            return;
        }

        loaded = true;
        Plan = CopyPlan(plan);
        ExpandedIdeaText = GetCreativeIdeaText();
        SceneName = SceneManager.GetActiveScene().name;
        LatestAdjustmentText = GetLatestAdjustmentText();
        CorridorValidation = corridorValidation == null ? null : corridorValidation.Copy();

        StoredLevelDesignPlan storedPlan = new StoredLevelDesignPlan
        {
            plan = Plan,
            expandedIdeaText = ExpandedIdeaText,
            sceneName = SceneName,
            latestAdjustmentText = LatestAdjustmentText,
            corridorValidation = CorridorValidation
        };

        PlayerPrefs.SetString(PlanPrefsKey, JsonUtility.ToJson(storedPlan));
        PlayerPrefs.Save();
    }

    public static bool TryGetPlan(out LevelDesignPlan plan)
    {
        EnsureLoaded();
        plan = Plan;
        return plan != null;
    }

    public static void Clear()
    {
        loaded = true;
        Plan = null;
        ExpandedIdeaText = "";
        SceneName = "";
        LatestAdjustmentText = "";
        CorridorValidation = null;

        PlayerPrefs.DeleteKey(PlanPrefsKey);
        PlayerPrefs.Save();
    }

    private static void EnsureLoaded()
    {
        if (loaded)
        {
            return;
        }

        loaded = true;
        string json = PlayerPrefs.GetString(PlanPrefsKey, "");

        if (string.IsNullOrEmpty(json))
        {
            Plan = null;
            ExpandedIdeaText = "";
            SceneName = "";
            LatestAdjustmentText = "";
            CorridorValidation = null;
            return;
        }

        try
        {
            StoredLevelDesignPlan storedPlan = JsonUtility.FromJson<StoredLevelDesignPlan>(json);

            if (storedPlan == null || storedPlan.plan == null)
            {
                Plan = null;
                ExpandedIdeaText = "";
                SceneName = "";
                LatestAdjustmentText = "";
                CorridorValidation = null;
                return;
            }

            Plan = CopyPlan(storedPlan.plan);
            ExpandedIdeaText = storedPlan.expandedIdeaText ?? "";
            SceneName = storedPlan.sceneName ?? "";
            LatestAdjustmentText = storedPlan.latestAdjustmentText ?? "";
            CorridorValidation = storedPlan.corridorValidation == null
                ? null
                : storedPlan.corridorValidation.Copy();
        }
        catch (System.Exception exception)
        {
            Debug.LogWarning("LevelDesignPlanContext could not load stored plan: " + exception.Message);
            Plan = null;
            ExpandedIdeaText = "";
            SceneName = "";
            LatestAdjustmentText = "";
            CorridorValidation = null;
        }
    }

    private static string GetCreativeIdeaText()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaText)
            ? CreativeWorkshopContext.IdeaText
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaTextPrefsKey, "");
    }

    private static string GetLatestAdjustmentText()
    {
        string revisionMode = PlayerPrefs.GetString(
            CreativeWorkshopContext.RevisionModePrefsKey,
            ""
        );

        if (!string.Equals(revisionMode, "ha", System.StringComparison.OrdinalIgnoreCase))
        {
            return PlayerPrefs.GetString(
                CreativeWorkshopContext.LatestAdjustmentTextPrefsKey,
                ""
            );
        }

        string selectedPlanJson = PlayerPrefs.GetString(
            CreativeWorkshopContext.SelectedHAPlanPrefsKey,
            ""
        );

        if (string.IsNullOrWhiteSpace(selectedPlanJson))
        {
            return "";
        }

        try
        {
            SelectedHAPlanSummary selectedPlan =
                JsonUtility.FromJson<SelectedHAPlanSummary>(selectedPlanJson);
            return selectedPlan == null || string.IsNullOrWhiteSpace(selectedPlan.description)
                ? ""
                : selectedPlan.description.Trim();
        }
        catch (System.Exception exception)
        {
            Debug.LogWarning(
                "LevelDesignPlanContext could not load the selected HA plan: "
                + exception.Message
            );
            return "";
        }
    }

    private static LevelDesignPlan CopyPlan(LevelDesignPlan source)
    {
        if (source == null)
        {
            return null;
        }

        return new LevelDesignPlan
        {
            minSolutionSteps = source.minSolutionSteps,
            maxSolutionSteps = source.maxSolutionSteps,
            minWaterAreas = source.minWaterAreas,
            maxWaterAreas = source.maxWaterAreas,
            minWallObstacleBlocks = source.minWallObstacleBlocks,
            maxWallObstacleBlocks = source.maxWallObstacleBlocks,
            minPushes = source.minPushes,
            maxPushes = source.maxPushes,
            minReversePulls = source.minReversePulls,
            maxReversePulls = source.maxReversePulls,
            style = source.style,
            archetype = source.archetype,
            targetLayout = source.targetLayout,
            obstacleStyle = source.obstacleStyle,
            waterStyle = source.waterStyle,
            designNote = source.designNote,
            corridorPlacement = source.corridorPlacement,
            corridorWidth = source.corridorWidth,
            corridorOrientation = source.corridorOrientation,
            corridorRole = source.corridorRole,
            corridorPriority = source.corridorPriority
        };
    }

    [System.Serializable]
    private class StoredLevelDesignPlan
    {
        public LevelDesignPlan plan;
        public string expandedIdeaText;
        public string sceneName;
        public string latestAdjustmentText;
        public CorridorValidationResult corridorValidation;
    }

    [System.Serializable]
    private class SelectedHAPlanSummary
    {
        public string description = "";
    }
}
