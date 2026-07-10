using System;
using System.Text;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class DesignInterpretationController : MonoBehaviour
{
    [Header("Scene UI")]
    public Text interpretationText;
    public Button backButton;

    [Header("Flow")]
    public string refinementSceneName = "Refinement";

    [Header("Fallback")]
    public string noPlanMessage = "No LLM design plan is available for this level.";

    private void Start()
    {
        ResolveSceneReferences();
        WireButtons();
        RefreshInterpretation();
    }

    private void ResolveSceneReferences()
    {
        if (interpretationText == null)
        {
            GameObject textObject = GameObject.Find("DesignInterpretationText");

            if (textObject == null)
            {
                textObject = GameObject.Find("BodyText");
            }

            if (textObject != null)
            {
                interpretationText = textObject.GetComponent<Text>();
            }
        }

        if (backButton == null)
        {
            GameObject backObject = GameObject.Find("BackButton");

            if (backObject != null)
            {
                backButton = backObject.GetComponent<Button>();
            }
        }

        if (backButton == null)
        {
            backButton = FindButtonByText("Back");
        }
    }

    private void WireButtons()
    {
        if (backButton != null)
        {
            backButton.onClick.RemoveAllListeners();
            backButton.onClick.AddListener(LoadRefinement);
            backButton.interactable = true;
        }
    }

    private void RefreshInterpretation()
    {
        if (interpretationText == null)
        {
            return;
        }

        LevelDesignPlan plan;

        if (!LevelDesignPlanContext.TryGetPlan(out plan) || plan == null)
        {
            interpretationText.text = noPlanMessage;
            return;
        }

        interpretationText.text = BuildInterpretationText(plan);
    }

    private string BuildInterpretationText(LevelDesignPlan plan)
    {
        StringBuilder builder = new StringBuilder();

        AppendSection(builder, "Expanded Direction", GetExpandedDirectionText());
        AppendSection(builder, "Design Reasoning", GetDesignReasoning(plan));
        AppendSection(builder, "Map Blueprint", BuildBlueprintText(plan));
        AppendSection(builder, "Map Parameters", BuildParameterText(plan));

        return builder.ToString().TrimEnd();
    }

    private string GetExpandedDirectionText()
    {
        string expandedIdeaText = CleanText(LevelDesignPlanContext.ExpandedIdeaText);

        if (string.IsNullOrEmpty(expandedIdeaText))
        {
            return "No expanded direction text was stored for this level.";
        }

        return expandedIdeaText;
    }

    private string GetDesignReasoning(LevelDesignPlan plan)
    {
        string designNote = CleanText(plan.designNote);

        if (!string.IsNullOrEmpty(designNote))
        {
            return designNote;
        }

        return "The generator used a " + PrettyValue(plan.archetype)
            + " structure with a " + PrettyValue(plan.targetLayout)
            + " layout, then shaped the internal walls as " + PrettyValue(plan.obstacleStyle)
            + " and water as " + PrettyValue(plan.waterStyle)
            + " to express the selected direction.";
    }

    private string BuildBlueprintText(LevelDesignPlan plan)
    {
        return "Style: " + PrettyValue(plan.style) + "\n"
            + "Archetype: " + PrettyValue(plan.archetype) + "\n"
            + "Target layout: " + PrettyValue(plan.targetLayout) + "\n"
            + "Obstacle style: " + PrettyValue(plan.obstacleStyle) + "\n"
            + "Water style: " + PrettyValue(plan.waterStyle);
    }

    private string BuildParameterText(LevelDesignPlan plan)
    {
        return "Solution steps: " + FormatRange(plan.minSolutionSteps, plan.maxSolutionSteps) + "\n"
            + "Pushes: " + FormatRange(plan.minPushes, plan.maxPushes) + "\n"
            + "Reverse pulls: " + FormatRange(plan.minReversePulls, plan.maxReversePulls) + "\n"
            + "Water areas: " + FormatRange(plan.minWaterAreas, plan.maxWaterAreas) + "\n"
            + "Wall obstacle blocks: " + FormatRange(plan.minWallObstacleBlocks, plan.maxWallObstacleBlocks);
    }

    private void AppendSection(StringBuilder builder, string title, string body)
    {
        if (builder.Length > 0)
        {
            builder.Append("\n\n");
        }

        builder.Append("<b>");
        builder.Append(title);
        builder.Append("</b>\n");
        builder.Append(body);
    }

    private Button FindButtonByText(string text)
    {
        Button[] buttons = FindObjectsOfType<Button>();

        for (int i = 0; i < buttons.Length; i++)
        {
            Button button = buttons[i];

            if (button == null)
            {
                continue;
            }

            Text buttonText = button.GetComponentInChildren<Text>();

            if (buttonText != null && CleanText(buttonText.text) == text)
            {
                return button;
            }
        }

        return null;
    }

    private void LoadRefinement()
    {
        if (string.IsNullOrEmpty(refinementSceneName))
        {
            Debug.LogWarning("DesignInterpretationController: refinement scene name is empty.");
            return;
        }

        SceneManager.LoadScene(refinementSceneName);
    }

    private string FormatRange(int min, int max)
    {
        if (min <= 0 && max <= 0)
        {
            return "Not specified";
        }

        if (max <= 0)
        {
            return min + "+";
        }

        if (min <= 0)
        {
            return "Up to " + max;
        }

        if (min == max)
        {
            return min.ToString();
        }

        return min + "-" + max;
    }

    private string PrettyValue(string value)
    {
        string cleanValue = CleanText(value);

        if (string.IsNullOrEmpty(cleanValue))
        {
            return "Not specified";
        }

        return cleanValue.Replace("_", " ");
    }

    private string CleanText(string value)
    {
        return string.IsNullOrEmpty(value)
            ? ""
            : string.Join(" ", value.Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries));
    }
}
