using UnityEngine;
using UnityEngine.SceneManagement;

public static class LevelDesignPlanContext
{
    private const string PlanPrefsKey = "SokobanLastLevelDesignPlan";

    private static bool loaded;

    public static LevelDesignPlan Plan { get; private set; }
    public static string ExpandedIdeaText { get; private set; }
    public static string SceneName { get; private set; }

    public static bool HasPlan
    {
        get
        {
            EnsureLoaded();
            return Plan != null;
        }
    }

    public static void SaveAppliedPlan(LevelDesignPlan plan)
    {
        if (plan == null)
        {
            Clear();
            return;
        }

        loaded = true;
        Plan = CopyPlan(plan);
        ExpandedIdeaText = GetCreativeIdeaText();
        SceneName = SceneManager.GetActiveScene().name;

        StoredLevelDesignPlan storedPlan = new StoredLevelDesignPlan
        {
            plan = Plan,
            expandedIdeaText = ExpandedIdeaText,
            sceneName = SceneName
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
                return;
            }

            Plan = CopyPlan(storedPlan.plan);
            ExpandedIdeaText = storedPlan.expandedIdeaText ?? "";
            SceneName = storedPlan.sceneName ?? "";
        }
        catch (System.Exception exception)
        {
            Debug.LogWarning("LevelDesignPlanContext could not load stored plan: " + exception.Message);
            Plan = null;
            ExpandedIdeaText = "";
            SceneName = "";
        }
    }

    private static string GetCreativeIdeaText()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaText)
            ? CreativeWorkshopContext.IdeaText
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaTextPrefsKey, "");
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
            designNote = source.designNote
        };
    }

    [System.Serializable]
    private class StoredLevelDesignPlan
    {
        public LevelDesignPlan plan;
        public string expandedIdeaText;
        public string sceneName;
    }
}
