using System;
using System.Text;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class DesignInterpretationController : MonoBehaviour
{
    private static string requestedReturnSceneName = "";

    [Header("Scene UI")]
    public Text interpretationText;
    public Button backButton;
    public ScrollRect interpretationScrollRect;
    public Scrollbar verticalScrollbar;

    [Header("Scroll Box")]
    public float scrollbarWidth = 18f;
    public float scrollbarSpacing = 10f;
    public float textHorizontalPadding = 16f;

    [Header("Flow")]
    public string refinementSceneName = "Refinement";

    [Header("Fallback")]
    public string noPlanMessage = "No LLM design plan is available for this level.";

    private void Start()
    {
        ResolveSceneReferences();
        WireButtons();
        EnsureScrollableTextBox();
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

    private void EnsureScrollableTextBox()
    {
        if (interpretationText == null)
        {
            return;
        }

        RectTransform textRect = interpretationText.rectTransform;

        if (interpretationScrollRect != null)
        {
            ConfigureExistingScrollRect(textRect);
            return;
        }

        RectTransform originalParent = textRect.parent as RectTransform;

        if (originalParent == null)
        {
            return;
        }

        int originalSiblingIndex = textRect.GetSiblingIndex();

        GameObject scrollViewObject = new GameObject(
            "DesignInterpretationScrollView",
            typeof(RectTransform),
            typeof(Image),
            typeof(ScrollRect)
        );
        RectTransform scrollViewRect = scrollViewObject.GetComponent<RectTransform>();
        scrollViewRect.SetParent(originalParent, false);
        CopyRectTransformLayout(textRect, scrollViewRect);
        scrollViewRect.SetSiblingIndex(originalSiblingIndex);

        Image scrollViewImage = scrollViewObject.GetComponent<Image>();
        scrollViewImage.color = new Color(1f, 1f, 1f, 0f);
        scrollViewImage.raycastTarget = true;

        GameObject viewportObject = new GameObject(
            "Viewport",
            typeof(RectTransform),
            typeof(Image),
            typeof(RectMask2D)
        );
        RectTransform viewportRect = viewportObject.GetComponent<RectTransform>();
        viewportRect.SetParent(scrollViewRect, false);
        StretchToParent(viewportRect);
        viewportRect.offsetMin = Vector2.zero;
        viewportRect.offsetMax = new Vector2(
            -Mathf.Max(0f, scrollbarWidth + scrollbarSpacing),
            0f
        );

        Image viewportImage = viewportObject.GetComponent<Image>();
        viewportImage.color = new Color(1f, 1f, 1f, 0f);
        viewportImage.raycastTarget = true;

        textRect.SetParent(viewportRect, false);
        ConfigureContentRect(textRect);

        interpretationScrollRect = scrollViewObject.GetComponent<ScrollRect>();
        interpretationScrollRect.viewport = viewportRect;
        interpretationScrollRect.content = textRect;
        ConfigureScrollRect(interpretationScrollRect);

        if (verticalScrollbar == null)
        {
            verticalScrollbar = CreateVerticalScrollbar(scrollViewRect);
        }

        ConfigureScrollbar();
        ResetScrollPosition();
    }

    private void ConfigureExistingScrollRect(RectTransform textRect)
    {
        if (verticalScrollbar == null)
        {
            verticalScrollbar = interpretationScrollRect.verticalScrollbar;
        }

        if (verticalScrollbar == null)
        {
            RectTransform scrollViewRect = interpretationScrollRect.transform as RectTransform;

            if (scrollViewRect != null)
            {
                verticalScrollbar = CreateVerticalScrollbar(scrollViewRect);
            }
        }

        if (interpretationScrollRect.viewport == null)
        {
            interpretationScrollRect.viewport = textRect.parent as RectTransform;
        }

        interpretationScrollRect.content = textRect;
        ConfigureScrollRect(interpretationScrollRect);

        if (verticalScrollbar != null)
        {
            ConfigureScrollbar();
        }

        ConfigureContentRect(textRect);
        ResetScrollPosition();
    }

    private void ConfigureScrollRect(ScrollRect scrollRect)
    {
        scrollRect.horizontal = false;
        scrollRect.vertical = true;
        scrollRect.movementType = ScrollRect.MovementType.Clamped;
        scrollRect.inertia = true;
        scrollRect.scrollSensitivity = 32f;
        scrollRect.verticalScrollbar = verticalScrollbar;
        scrollRect.verticalScrollbarVisibility = ScrollRect.ScrollbarVisibility.Permanent;
        scrollRect.horizontalScrollbar = null;
        scrollRect.horizontalScrollbarVisibility = ScrollRect.ScrollbarVisibility.Permanent;
    }

    private Scrollbar CreateVerticalScrollbar(RectTransform scrollViewRect)
    {
        GameObject scrollbarObject = new GameObject(
            "VerticalScrollbar",
            typeof(RectTransform),
            typeof(Image),
            typeof(Scrollbar)
        );
        RectTransform scrollbarRect = scrollbarObject.GetComponent<RectTransform>();
        scrollbarRect.SetParent(scrollViewRect, false);
        scrollbarRect.anchorMin = new Vector2(1f, 0f);
        scrollbarRect.anchorMax = new Vector2(1f, 1f);
        scrollbarRect.pivot = new Vector2(1f, 0.5f);
        scrollbarRect.anchoredPosition = Vector2.zero;
        scrollbarRect.sizeDelta = new Vector2(Mathf.Max(4f, scrollbarWidth), 0f);

        Image trackImage = scrollbarObject.GetComponent<Image>();
        trackImage.color = new Color(0.08f, 0.12f, 0.18f, 0.18f);
        trackImage.raycastTarget = true;

        GameObject handleObject = new GameObject(
            "Handle",
            typeof(RectTransform),
            typeof(Image)
        );
        RectTransform handleRect = handleObject.GetComponent<RectTransform>();
        handleRect.SetParent(scrollbarRect, false);
        StretchToParent(handleRect);

        Image handleImage = handleObject.GetComponent<Image>();
        handleImage.color = new Color(0.1f, 0.35f, 0.75f, 0.85f);
        handleImage.raycastTarget = true;

        Scrollbar scrollbar = scrollbarObject.GetComponent<Scrollbar>();
        scrollbar.direction = Scrollbar.Direction.BottomToTop;
        scrollbar.targetGraphic = handleImage;
        scrollbar.handleRect = handleRect;
        scrollbar.size = 1f;

        return scrollbar;
    }

    private void ConfigureScrollbar()
    {
        if (interpretationScrollRect == null || verticalScrollbar == null)
        {
            return;
        }

        interpretationScrollRect.verticalScrollbar = verticalScrollbar;
        interpretationScrollRect.verticalScrollbarVisibility = ScrollRect.ScrollbarVisibility.Permanent;
        verticalScrollbar.gameObject.SetActive(true);
    }

    private void ConfigureContentRect(RectTransform textRect)
    {
        textRect.anchorMin = new Vector2(0f, 1f);
        textRect.anchorMax = new Vector2(1f, 1f);
        textRect.pivot = new Vector2(0f, 1f);
        textRect.anchoredPosition = new Vector2(Mathf.Max(0f, textHorizontalPadding), 0f);
        textRect.sizeDelta = new Vector2(-Mathf.Max(0f, textHorizontalPadding) * 2f, textRect.sizeDelta.y);

        interpretationText.horizontalOverflow = HorizontalWrapMode.Wrap;
        interpretationText.verticalOverflow = VerticalWrapMode.Overflow;

        ContentSizeFitter sizeFitter = interpretationText.GetComponent<ContentSizeFitter>();

        if (sizeFitter == null)
        {
            sizeFitter = interpretationText.gameObject.AddComponent<ContentSizeFitter>();
        }

        sizeFitter.horizontalFit = ContentSizeFitter.FitMode.Unconstrained;
        sizeFitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
    }

    private void CopyRectTransformLayout(RectTransform source, RectTransform target)
    {
        target.anchorMin = source.anchorMin;
        target.anchorMax = source.anchorMax;
        target.pivot = source.pivot;
        target.anchoredPosition = source.anchoredPosition;
        target.sizeDelta = source.sizeDelta;
        target.localRotation = source.localRotation;
        target.localScale = source.localScale;
    }

    private void StretchToParent(RectTransform rectTransform)
    {
        rectTransform.anchorMin = Vector2.zero;
        rectTransform.anchorMax = Vector2.one;
        rectTransform.pivot = new Vector2(0.5f, 0.5f);
        rectTransform.anchoredPosition = Vector2.zero;
        rectTransform.sizeDelta = Vector2.zero;
    }

    private void SetInterpretationText(string value)
    {
        interpretationText.text = value;
        ResetScrollPosition();
    }

    private void ResetScrollPosition()
    {
        if (interpretationText != null)
        {
            LayoutRebuilder.ForceRebuildLayoutImmediate(interpretationText.rectTransform);
        }

        if (interpretationScrollRect == null)
        {
            return;
        }

        Canvas.ForceUpdateCanvases();
        interpretationScrollRect.verticalNormalizedPosition = 1f;
        interpretationScrollRect.StopMovement();
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
            SetInterpretationText(noPlanMessage);
            return;
        }

        SetInterpretationText(BuildInterpretationText(plan));
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
        string returnSceneName = string.IsNullOrEmpty(requestedReturnSceneName)
            ? refinementSceneName
            : requestedReturnSceneName;
        requestedReturnSceneName = "";

        if (string.IsNullOrEmpty(returnSceneName))
        {
            Debug.LogWarning("DesignInterpretationController: return scene name is empty.");
            return;
        }

        SceneManager.LoadScene(returnSceneName);
    }

    public static void SetReturnScene(string sceneName)
    {
        requestedReturnSceneName = string.IsNullOrEmpty(sceneName) ? "" : sceneName.Trim();
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
