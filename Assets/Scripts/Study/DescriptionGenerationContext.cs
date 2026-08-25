using System;
using UnityEngine;

[Serializable]
public class LevelGenerationPreferences
{
    public int minSolutionSteps = 22;
    public int maxSolutionSteps = 42;
    public int minPushes = 10;
    public int maxPushes = 22;
    public int minReversePulls = 18;
    public int maxReversePulls = 34;
    public int minWaterAreas = -1;
    public int maxWaterAreas = -1;
    public int minWallObstacleBlocks = -1;
    public int maxWallObstacleBlocks = -1;
    public string archetype = "";
    public string targetLayout = "";
    public string obstacleStyle = "";
    public string waterStyle = "";
    public string corridorPlacement = "";
    public int corridorWidth = -1;
    public string corridorOrientation = "";
    public string corridorRole = "";
    public string corridorPriority = "";
}

[Serializable]
public class DescriptionGenerationSettings
{
    public string styleDescription = "";
    public string firstMovePreference = "";
    public string pushPlanningPreference = "";
    public string spacePreference = "";
    public string routeRhythmPreference = "";
    public string aiSummary = "";
    public string aiDifficultyRationale = "";
    public string aiLayoutRationale = "";
    public string aiRecommendedDifficulty = "";
    public string aiRecommendedLayout = "";
    public string aiRecommendationSource = "";
    public string finalDifficulty = "";
    public string finalLayout = "";
    public LevelGenerationPreferences preferences =
        new LevelGenerationPreferences();
}

public static class DescriptionGenerationContext
{
    public const string SettingsPrefsKey =
        "SokobanDescriptionGenerationSettings";

    public static void Save(DescriptionGenerationSettings settings)
    {
        if (settings == null)
        {
            return;
        }

        if (settings.preferences == null)
        {
            settings.preferences = new LevelGenerationPreferences();
        }

        PlayerPrefs.SetString(SettingsPrefsKey, JsonUtility.ToJson(settings));
        PlayerPrefs.Save();
    }

    public static bool TryLoad(out DescriptionGenerationSettings settings)
    {
        settings = null;
        string json = PlayerPrefs.GetString(SettingsPrefsKey, "");

        if (string.IsNullOrWhiteSpace(json))
        {
            return false;
        }

        try
        {
            settings = JsonUtility.FromJson<DescriptionGenerationSettings>(json);

            if (settings == null)
            {
                return false;
            }

            if (settings.preferences == null)
            {
                settings.preferences = new LevelGenerationPreferences();
            }

            return true;
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                "DescriptionGenerationContext: Could not load settings: "
                + exception.Message
            );
            settings = null;
            return false;
        }
    }

    public static void Clear()
    {
        PlayerPrefs.DeleteKey(SettingsPrefsKey);
        PlayerPrefs.Save();
    }

}
