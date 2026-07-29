using System;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class DescriptionGenerationController : MonoBehaviour
{
    private const int MaxDescriptionLength = 420;

    private static readonly Color TextColor =
        new Color(0.12f, 0.16f, 0.22f, 1f);

    [Header("Navigation")]
    [Tooltip("Leave empty until the dedicated result scene is ready.")]
    public string nextSceneName = "LLM_Level";

    [Header("Initial Values")]
    [Tooltip(
        "When disabled, Play Mode starts from the values visible in the scene."
    )]
    [SerializeField] private bool restoreSavedSettings;

    private DescriptionGenerationSettings settings;
    [SerializeField] private Text statusText;
    [SerializeField] private Button generateButton;
    [SerializeField] private Button advancedToggleButton;
    [SerializeField] private Text advancedToggleText;
    [SerializeField] private GameObject advancedPanel;
    [SerializeField] private InputField descriptionInput;
    [SerializeField] private InputField minSolutionStepsInput;
    [SerializeField] private InputField maxSolutionStepsInput;
    [SerializeField] private InputField minPushesInput;
    [SerializeField] private InputField maxPushesInput;
    [SerializeField] private InputField minReversePullsInput;
    [SerializeField] private InputField maxReversePullsInput;
    private Transform settingsContent;
    private SelectorBinding difficultySelector;
    private SelectorBinding corridorSelector;
    private SelectorBinding corridorOrientationSelector;
    private SelectorBinding corridorRoleSelector;
    private SelectorBinding corridorPrioritySelector;
    private bool advancedVisible;

    private sealed class SelectorBinding
    {
        public string[] labels;
        public string[] values;
        public Text valueText;
        public Button previousButton;
        public Button nextButton;
        public Action<int> onChanged;
        public int index;

        public void Change(int direction)
        {
            int count = labels != null ? labels.Length : 0;

            if (count == 0)
            {
                return;
            }

            SetIndex((index + direction + count) % count, true);
        }

        public void SetIndex(int value, bool notify)
        {
            int count = labels != null ? labels.Length : 0;

            if (count == 0)
            {
                return;
            }

            index = Mathf.Clamp(value, 0, count - 1);

            if (valueText != null)
            {
                valueText.text = labels[index];
            }

            if (notify)
            {
                onChanged?.Invoke(index);
            }
        }

        public void SetInteractable(bool interactable)
        {
            if (previousButton != null)
            {
                previousButton.interactable = interactable;
            }

            if (nextButton != null)
            {
                nextButton.interactable = interactable;
            }

            if (valueText != null)
            {
                valueText.color = interactable
                    ? TextColor
                    : new Color(TextColor.r, TextColor.g, TextColor.b, 0.45f);
            }
        }
    }

    private void Start()
    {
        if (!BindSerializedInterface())
        {
            Debug.LogError(
                "DescriptionGenerationController: The serialized DG form is "
                    + "missing. Open and save the DG scene in Unity Editor."
            );
            enabled = false;
            return;
        }

        settings = restoreSavedSettings
            && DescriptionGenerationContext.TryLoad(
                out DescriptionGenerationSettings savedSettings
            )
                ? savedSettings
                : CaptureSceneSettings();

        ConfigureRuntimeInterface();
        ValidateAndUpdateStatus(false);
    }

    private bool BindSerializedInterface()
    {
        Canvas canvas = FindObjectOfType<Canvas>();

        if (canvas == null)
        {
            return false;
        }

        Transform scrollView = canvas.transform.Find("SettingsScrollView");

        if (scrollView == null)
        {
            Transform legacyRoot = canvas.transform.Find(
                "DescriptionGenerationPanel"
            );
            Transform legacyCard = legacyRoot != null
                ? legacyRoot.Find("Card")
                : null;
            scrollView = legacyCard != null
                ? legacyCard.Find("SettingsScrollView")
                : null;
        }

        Transform content = scrollView != null
            ? scrollView.Find("Viewport/Content")
            : null;

        if (content == null)
        {
            Transform viewport = canvas.transform.Find("Viewport");
            content = viewport != null ? viewport.Find("Content") : null;
        }

        if (content == null && descriptionInput != null)
        {
            Transform current = descriptionInput.transform;

            while (current != null && current != canvas.transform)
            {
                if (current.name == "Content")
                {
                    content = current;
                    break;
                }

                current = current.parent;
            }
        }

        if (content == null)
        {
            return false;
        }

        settingsContent = content;
        advancedPanel = FindGameObject(content, "AdvancedParameters");
        advancedToggleButton = FindComponent<Button>(
            content,
            "AdvancedToggle"
        );
        advancedToggleText = advancedToggleButton != null
            ? advancedToggleButton.GetComponentInChildren<Text>(true)
            : null;
        descriptionInput = FindComponent<InputField>(
            content,
            "DescriptionInputRow/InputField"
        );

        if (descriptionInput != null && descriptionInput.textComponent == null)
        {
            // Keep the serialized scene UI usable if Unity clears the link.
            descriptionInput.textComponent = FindComponent<Text>(
                descriptionInput.transform,
                "Text"
            );
        }

        statusText = null;
        GameObject confirmObject = GameObject.Find("ConfirmButton");
        generateButton = confirmObject != null
            ? confirmObject.GetComponent<Button>()
            : null;

        minSolutionStepsInput = null;
        maxSolutionStepsInput = null;
        minPushesInput = null;
        maxPushesInput = null;
        minReversePullsInput = null;
        maxReversePullsInput = null;

        if (advancedPanel != null)
        {
            BindRangeInputs(
                advancedPanel.transform,
                "SolutionstepsRow/Range",
                out minSolutionStepsInput,
                out maxSolutionStepsInput
            );
            BindRangeInputs(
                advancedPanel.transform,
                "BoxpushesRow/Range",
                out minPushesInput,
                out maxPushesInput
            );
            BindRangeInputs(
                advancedPanel.transform,
                "ReversepullsRow/Range",
                out minReversePullsInput,
                out maxReversePullsInput
            );
        }

        return descriptionInput != null
            && descriptionInput.textComponent != null;
    }

    private DescriptionGenerationSettings CaptureSceneSettings()
    {
        DescriptionGenerationSettings captured =
            new DescriptionGenerationSettings();
        LevelGenerationPreferences preferences = captured.preferences;

        int difficulty = ReadSceneSelectorIndex(
            "DifficultyRow/Selector",
            new[] { "Easy", "Medium", "Hard", "Custom" },
            1
        );

        if (difficulty == 0)
        {
            SetDifficultyValues(preferences, 18, 32, 8, 14, 14, 24);
        }
        else if (difficulty == 2)
        {
            SetDifficultyValues(preferences, 30, 50, 16, 28, 24, 40);
        }
        else
        {
            SetDifficultyValues(preferences, 22, 42, 10, 22, 18, 34);
        }

        string[] archetypes =
        {
            "",
            "goal_room",
            "bottleneck_corridor",
            "split_route",
            "open_workshop"
        };
        int archetype = ReadSceneSelectorIndex(
            "LevelstructureRow/Selector",
            new[]
            {
                "Auto",
                "Goal room",
                "Bottleneck corridor",
                "Split route",
                "Open workshop"
            },
            0
        );
        preferences.archetype = archetypes[archetype];

        string[] targetLayouts =
        {
            "",
            "clustered",
            "split_pair",
            "edge_cluster"
        };
        int targetLayout = ReadSceneSelectorIndex(
            "TargetlayoutRow/Selector",
            new[] { "Auto", "Clustered", "Split pair", "Edge cluster" },
            0
        );
        preferences.targetLayout = targetLayouts[targetLayout];

        int water = ReadSceneSelectorIndex(
            "WaterareasRow/Selector",
            new[] { "Auto", "None", "1 area", "2 areas" },
            0
        );
        preferences.minWaterAreas = water == 0 ? -1 : water - 1;
        preferences.maxWaterAreas = water == 0 ? -1 : water - 1;

        int walls = ReadSceneSelectorIndex(
            "InternalobstaclesRow/Selector",
            new[]
            {
                "Auto",
                "None",
                "1 block",
                "2 blocks",
                "2-3 blocks"
            },
            0
        );

        if (walls == 0)
        {
            preferences.minWallObstacleBlocks = -1;
            preferences.maxWallObstacleBlocks = -1;
        }
        else if (walls == 4)
        {
            preferences.minWallObstacleBlocks = 2;
            preferences.maxWallObstacleBlocks = 3;
        }
        else
        {
            preferences.minWallObstacleBlocks = walls - 1;
            preferences.maxWallObstacleBlocks = walls - 1;
        }

        captured.styleDescription = descriptionInput.text ?? "";
        return captured;
    }

    private int ReadSceneSelectorIndex(
        string path,
        string[] labels,
        int fallback
    )
    {
        Text valueText = FindComponent<Text>(
            settingsContent,
            path + "/ValuePanel/Value"
        );

        if (valueText == null)
        {
            return fallback;
        }

        for (int i = 0; i < labels.Length; i++)
        {
            if (string.Equals(
                    labels[i],
                    valueText.text,
                    StringComparison.OrdinalIgnoreCase
                ))
            {
                return i;
            }
        }

        return fallback;
    }

    private void SetDifficultyValues(
        LevelGenerationPreferences preferences,
        int minSteps,
        int maxSteps,
        int minPushes,
        int maxPushes,
        int minPulls,
        int maxPulls
    )
    {
        preferences.minSolutionSteps = minSteps;
        preferences.maxSolutionSteps = maxSteps;
        preferences.minPushes = minPushes;
        preferences.maxPushes = maxPushes;
        preferences.minReversePulls = minPulls;
        preferences.maxReversePulls = maxPulls;
    }

    private void ConfigureRuntimeInterface()
    {
        Transform content = settingsContent;
        LevelGenerationPreferences preferences = settings.preferences;

        difficultySelector = BindSelector(
            content,
            "DifficultyRow/Selector",
            new[] { "Easy", "Medium", "Hard", "Custom" },
            new[] { "easy", "medium", "hard", "custom" },
            ResolveDifficultyIndex(),
            OnDifficultyChanged
        );

        BindSelector(
            content,
            "LevelstructureRow/Selector",
            new[]
            {
                "Auto",
                "Goal room",
                "Bottleneck corridor",
                "Split route",
                "Open workshop"
            },
            new[]
            {
                "",
                "goal_room",
                "bottleneck_corridor",
                "split_route",
                "open_workshop"
            },
            FindValueIndex(
                new[]
                {
                    "",
                    "goal_room",
                    "bottleneck_corridor",
                    "split_route",
                    "open_workshop"
                },
                preferences.archetype,
                0
            ),
            index =>
            {
                preferences.archetype =
                    new[]
                    {
                        "",
                        "goal_room",
                        "bottleneck_corridor",
                        "split_route",
                        "open_workshop"
                    }[index];
                ValidateAndUpdateStatus(false);
            }
        );

        BindSelector(
            content,
            "TargetlayoutRow/Selector",
            new[] { "Auto", "Clustered", "Split pair", "Edge cluster" },
            new[] { "", "clustered", "split_pair", "edge_cluster" },
            FindValueIndex(
                new[] { "", "clustered", "split_pair", "edge_cluster" },
                preferences.targetLayout,
                0
            ),
            index =>
            {
                preferences.targetLayout =
                    new[] { "", "clustered", "split_pair", "edge_cluster" }[
                        index
                    ];
                ValidateAndUpdateStatus(false);
            }
        );

        BindSelector(
            content,
            "WaterareasRow/Selector",
            new[] { "Auto", "None", "1 area", "2 areas" },
            new[] { "auto", "0", "1", "2" },
            ResolveWaterIndex(),
            OnWaterChanged
        );

        BindSelector(
            content,
            "InternalobstaclesRow/Selector",
            new[]
            {
                "Auto",
                "None",
                "1 block",
                "2 blocks",
                "2-3 blocks"
            },
            new[] { "auto", "0", "1", "2", "2-3" },
            ResolveWallIndex(),
            OnWallChanged
        );

        if (advancedPanel != null)
        {
            Transform advanced = advancedPanel.transform;
            BindSelector(
            advanced,
            "ObstaclestyleRow/Selector",
            new[]
            {
                "Auto",
                "Central baffle",
                "Side choke",
                "Goal guard"
            },
            new[]
            {
                "",
                "central_baffle",
                "side_choke",
                "goal_guard"
            },
            FindValueIndex(
                new[]
                {
                    "",
                    "central_baffle",
                    "side_choke",
                    "goal_guard"
                },
                preferences.obstacleStyle,
                0
            ),
            index =>
            {
                preferences.obstacleStyle =
                    new[]
                    {
                        "",
                        "central_baffle",
                        "side_choke",
                        "goal_guard"
                    }[index];
                ValidateAndUpdateStatus(false);
            }
        );

        BindSelector(
            advanced,
            "WaterstyleRow/Selector",
            new[]
            {
                "Auto",
                "Corner pool",
                "Side pool",
                "Route divider"
            },
            new[]
            {
                "",
                "corner_pool",
                "side_pool",
                "route_divider"
            },
            FindValueIndex(
                new[]
                {
                    "",
                    "corner_pool",
                    "side_pool",
                    "route_divider"
                },
                preferences.waterStyle,
                0
            ),
            index =>
            {
                preferences.waterStyle =
                    new[]
                    {
                        "",
                        "corner_pool",
                        "side_pool",
                        "route_divider"
                    }[index];
                ValidateAndUpdateStatus(false);
            }
        );

        corridorSelector = BindSelector(
            advanced,
            "CorridorRow/Selector",
            new[]
            {
                "Auto",
                "None",
                "Center / width 1",
                "Center / width 2",
                "Side / width 1",
                "Side / width 2"
            },
            new[]
            {
                "auto",
                "none",
                "center-1",
                "center-2",
                "side-1",
                "side-2"
            },
            ResolveCorridorIndex(),
            OnCorridorChanged
        );

        corridorOrientationSelector = BindSelector(
            advanced,
            "CorridororientationRow/Selector",
            new[] { "Any", "Horizontal", "Vertical" },
            new[] { "any", "horizontal", "vertical" },
            FindValueIndex(
                new[] { "any", "horizontal", "vertical" },
                preferences.corridorOrientation,
                0
            ),
            index =>
            {
                preferences.corridorOrientation =
                    new[] { "any", "horizontal", "vertical" }[index];
                ValidateAndUpdateStatus(false);
            }
        );

        corridorRoleSelector = BindSelector(
            advanced,
            "CorridorroleRow/Selector",
            new[] { "Player route", "Required box route", "Visual only" },
            new[] { "player_route", "required_box_route", "visual_only" },
            FindValueIndex(
                new[]
                {
                    "player_route",
                    "required_box_route",
                    "visual_only"
                },
                preferences.corridorRole,
                0
            ),
            index =>
            {
                preferences.corridorRole =
                    new[]
                    {
                        "player_route",
                        "required_box_route",
                        "visual_only"
                    }[index];
                ValidateAndUpdateStatus(false);
            }
        );

            corridorPrioritySelector = BindSelector(
            advanced,
            "CorridorpriorityRow/Selector",
            new[] { "Preferred", "Required" },
            new[] { "preferred", "required" },
            FindValueIndex(
                new[] { "preferred", "required" },
                preferences.corridorPriority,
                0
            ),
            index =>
            {
                preferences.corridorPriority =
                    new[] { "preferred", "required" }[index];
                ValidateAndUpdateStatus(false);
            }
            );
        }

        descriptionInput.onValueChanged.RemoveAllListeners();
        descriptionInput.onEndEdit.RemoveAllListeners();
        descriptionInput.text = settings.styleDescription ?? "";
        descriptionInput.onEndEdit.AddListener(
            value =>
            {
                settings.styleDescription = value;
                ValidateAndUpdateStatus(false);
            }
        );

        RefreshDifficultyInputs();
        WireNumericInput(minSolutionStepsInput);
        WireNumericInput(maxSolutionStepsInput);
        WireNumericInput(minPushesInput);
        WireNumericInput(maxPushesInput);
        WireNumericInput(minReversePullsInput);
        WireNumericInput(maxReversePullsInput);

        advancedVisible = false;

        if (advancedPanel != null && advancedToggleButton != null)
        {
            advancedPanel.SetActive(false);
            advancedToggleText.text = "Show advanced parameters";
            advancedToggleButton.onClick.RemoveAllListeners();
            advancedToggleButton.onClick.AddListener(ToggleAdvanced);
        }

        if (generateButton != null)
        {
            generateButton.onClick.RemoveAllListeners();
            generateButton.onClick.AddListener(SaveAndContinue);
            generateButton.interactable = true;
        }

        UpdateCorridorDependentControls();
    }

    private SelectorBinding BindSelector(
        Transform parent,
        string path,
        string[] labels,
        string[] values,
        int initialIndex,
        Action<int> onChanged
    )
    {
        Transform control = parent.Find(path);

        if (control == null)
        {
            return null;
        }

        Button previous = FindComponent<Button>(control, "Previous");
        Button next = FindComponent<Button>(control, "Next");
        Text valueText = FindComponent<Text>(control, "ValuePanel/Value");
        SelectorBinding binding = new SelectorBinding
        {
            labels = labels,
            values = values,
            previousButton = previous,
            nextButton = next,
            valueText = valueText,
            onChanged = onChanged
        };

        if (previous != null)
        {
            previous.onClick.RemoveAllListeners();
            previous.onClick.AddListener(() => binding.Change(-1));
        }

        if (next != null)
        {
            next.onClick.RemoveAllListeners();
            next.onClick.AddListener(() => binding.Change(1));
        }

        binding.SetIndex(initialIndex, false);
        return binding;
    }

    private void BindRangeInputs(
        Transform parent,
        string path,
        out InputField minimum,
        out InputField maximum
    )
    {
        minimum = null;
        maximum = null;
        Transform range = parent.Find(path);

        if (range == null)
        {
            return;
        }

        InputField[] fields = range.GetComponentsInChildren<InputField>(true);

        if (fields.Length >= 2)
        {
            minimum = fields[0];
            maximum = fields[1];
        }
    }

    private void WireNumericInput(InputField input)
    {
        if (input == null)
        {
            return;
        }

        input.onEndEdit.RemoveAllListeners();
        input.onEndEdit.AddListener(_ => OnNumericRangeEdited());
    }

    private T FindComponent<T>(Transform parent, string path)
        where T : Component
    {
        Transform child = parent != null ? parent.Find(path) : null;
        return child != null ? child.GetComponent<T>() : null;
    }

    private GameObject FindGameObject(Transform parent, string path)
    {
        Transform child = parent != null ? parent.Find(path) : null;
        return child != null ? child.gameObject : null;
    }


    private void OnDifficultyChanged(int index)
    {
        if (index == 0)
        {
            ApplyDifficultyPreset(18, 32, 8, 14, 14, 24);
        }
        else if (index == 1)
        {
            ApplyDifficultyPreset(22, 42, 10, 22, 18, 34);
        }
        else if (index == 2)
        {
            ApplyDifficultyPreset(30, 50, 16, 28, 24, 40);
        }

        ValidateAndUpdateStatus(false);
    }

    private void ApplyDifficultyPreset(
        int minSteps,
        int maxSteps,
        int minPushes,
        int maxPushes,
        int minPulls,
        int maxPulls
    )
    {
        LevelGenerationPreferences preferences = settings.preferences;
        preferences.minSolutionSteps = minSteps;
        preferences.maxSolutionSteps = maxSteps;
        preferences.minPushes = minPushes;
        preferences.maxPushes = maxPushes;
        preferences.minReversePulls = minPulls;
        preferences.maxReversePulls = maxPulls;
        RefreshDifficultyInputs();
    }

    private void RefreshDifficultyInputs()
    {
        if (minSolutionStepsInput == null)
        {
            return;
        }

        LevelGenerationPreferences preferences = settings.preferences;
        minSolutionStepsInput.text = preferences.minSolutionSteps.ToString();
        maxSolutionStepsInput.text = preferences.maxSolutionSteps.ToString();
        minPushesInput.text = preferences.minPushes.ToString();
        maxPushesInput.text = preferences.maxPushes.ToString();
        minReversePullsInput.text = preferences.minReversePulls.ToString();
        maxReversePullsInput.text = preferences.maxReversePulls.ToString();
    }

    private void OnWaterChanged(int index)
    {
        int value = index - 1;
        settings.preferences.minWaterAreas = index == 0 ? -1 : value;
        settings.preferences.maxWaterAreas = index == 0 ? -1 : value;
        ValidateAndUpdateStatus(false);
    }

    private void OnWallChanged(int index)
    {
        if (index == 0)
        {
            settings.preferences.minWallObstacleBlocks = -1;
            settings.preferences.maxWallObstacleBlocks = -1;
        }
        else if (index == 4)
        {
            settings.preferences.minWallObstacleBlocks = 2;
            settings.preferences.maxWallObstacleBlocks = 3;
        }
        else
        {
            int value = index - 1;
            settings.preferences.minWallObstacleBlocks = value;
            settings.preferences.maxWallObstacleBlocks = value;
        }

        ValidateAndUpdateStatus(false);
    }

    private void OnCorridorChanged(int index)
    {
        LevelGenerationPreferences preferences = settings.preferences;

        if (index == 0)
        {
            preferences.corridorPlacement = "";
            preferences.corridorWidth = -1;
            preferences.corridorOrientation = "";
            preferences.corridorRole = "";
            preferences.corridorPriority = "";
        }
        else if (index == 1)
        {
            preferences.corridorPlacement = "none";
            preferences.corridorWidth = 0;
            preferences.corridorOrientation = "any";
            preferences.corridorRole = "visual_only";
            preferences.corridorPriority = "preferred";
        }
        else
        {
            preferences.corridorPlacement = index <= 3 ? "center" : "side";
            preferences.corridorWidth = index % 2 == 0 ? 1 : 2;

            if (string.IsNullOrEmpty(preferences.corridorOrientation))
            {
                preferences.corridorOrientation = "any";
            }

            if (string.IsNullOrEmpty(preferences.corridorRole))
            {
                preferences.corridorRole = "player_route";
            }

            if (string.IsNullOrEmpty(preferences.corridorPriority))
            {
                preferences.corridorPriority = "preferred";
            }
        }

        SyncCorridorSelectors();
        UpdateCorridorDependentControls();
        ValidateAndUpdateStatus(false);
    }

    private void SyncCorridorSelectors()
    {
        if (corridorOrientationSelector == null)
        {
            return;
        }

        corridorOrientationSelector.SetIndex(
            FindValueIndex(
                corridorOrientationSelector.values,
                settings.preferences.corridorOrientation,
                0
            ),
            false
        );
        corridorRoleSelector.SetIndex(
            FindValueIndex(
                corridorRoleSelector.values,
                settings.preferences.corridorRole,
                0
            ),
            false
        );
        corridorPrioritySelector.SetIndex(
            FindValueIndex(
                corridorPrioritySelector.values,
                settings.preferences.corridorPriority,
                0
            ),
            false
        );
    }

    private void UpdateCorridorDependentControls()
    {
        bool enabled = settings.preferences.corridorPlacement == "center"
            || settings.preferences.corridorPlacement == "side";
        corridorOrientationSelector?.SetInteractable(enabled);
        corridorRoleSelector?.SetInteractable(enabled);
        corridorPrioritySelector?.SetInteractable(enabled);
    }

    private void ToggleAdvanced()
    {
        advancedVisible = !advancedVisible;
        advancedPanel.SetActive(advancedVisible);
        advancedToggleText.text = advancedVisible
            ? "Hide advanced parameters"
            : "Show advanced parameters";
        Canvas.ForceUpdateCanvases();
    }

    private void SaveAndContinue()
    {
        ValidateAndUpdateStatus(false);
        settings.styleDescription = (descriptionInput.text ?? "").Trim();
        DescriptionGenerationContext.Save(settings);

        if (!string.IsNullOrWhiteSpace(nextSceneName)
            && Application.CanStreamedLevelBeLoaded(nextSceneName))
        {
            SceneManager.LoadScene(nextSceneName);
            return;
        }

        Debug.LogError(
            "DescriptionGenerationController: Target scene is unavailable: "
                + nextSceneName
        );
    }

    private bool ValidateAndUpdateStatus(bool showSuccess)
    {
        if (!TryReadNumericFields(out string error))
        {
            SetStatus(error, true);
            return false;
        }

        if (settings.preferences.maxWallObstacleBlocks == 0
            && (settings.preferences.corridorPlacement == "center"
                || settings.preferences.corridorPlacement == "side"))
        {
            SetStatus(
                "A corridor cannot be required when internal obstacles are set "
                    + "to None.",
                true
            );
            return false;
        }

        SetStatus(
            showSuccess
                ? "Settings are valid."
                : "Manual parameters are hard constraints; the description "
                    + "guides style within those constraints.",
            false
        );
        settings.styleDescription = (descriptionInput.text ?? "").Trim();
        DescriptionGenerationContext.Save(settings);
        return true;
    }

    private bool TryReadNumericFields(out string error)
    {
        error = "";

        if (minSolutionStepsInput == null)
        {
            return true;
        }

        if (!TryReadRange(
                "Solution steps",
                minSolutionStepsInput,
                maxSolutionStepsInput,
                18,
                30,
                32,
                50,
                out int minSteps,
                out int maxSteps,
                out error
            )
            || !TryReadRange(
                "Box pushes",
                minPushesInput,
                maxPushesInput,
                8,
                16,
                14,
                28,
                out int minPushes,
                out int maxPushes,
                out error
            )
            || !TryReadRange(
                "Reverse pulls",
                minReversePullsInput,
                maxReversePullsInput,
                14,
                24,
                24,
                40,
                out int minPulls,
                out int maxPulls,
                out error
            ))
        {
            return false;
        }

        LevelGenerationPreferences preferences = settings.preferences;
        bool changed = preferences.minSolutionSteps != minSteps
            || preferences.maxSolutionSteps != maxSteps
            || preferences.minPushes != minPushes
            || preferences.maxPushes != maxPushes
            || preferences.minReversePulls != minPulls
            || preferences.maxReversePulls != maxPulls;

        preferences.minSolutionSteps = minSteps;
        preferences.maxSolutionSteps = maxSteps;
        preferences.minPushes = minPushes;
        preferences.maxPushes = maxPushes;
        preferences.minReversePulls = minPulls;
        preferences.maxReversePulls = maxPulls;

        if (changed && difficultySelector != null)
        {
            difficultySelector.SetIndex(ResolveDifficultyIndex(), false);
        }

        return true;
    }

    private bool TryReadRange(
        string label,
        InputField minInput,
        InputField maxInput,
        int minLower,
        int minUpper,
        int maxLower,
        int maxUpper,
        out int minValue,
        out int maxValue,
        out string error
    )
    {
        minValue = 0;
        maxValue = 0;

        if (!int.TryParse(minInput.text, out minValue)
            || !int.TryParse(maxInput.text, out maxValue))
        {
            error = label + " must use whole numbers.";
            return false;
        }

        if (minValue < minLower || minValue > minUpper)
        {
            error = label + " minimum must be " + minLower + "-" + minUpper + ".";
            return false;
        }

        if (maxValue < maxLower || maxValue > maxUpper)
        {
            error = label + " maximum must be " + maxLower + "-" + maxUpper + ".";
            return false;
        }

        if (maxValue < minValue)
        {
            error = label + " maximum cannot be lower than its minimum.";
            return false;
        }

        error = "";
        return true;
    }

    private int ResolveDifficultyIndex()
    {
        LevelGenerationPreferences preferences = settings.preferences;

        if (MatchesDifficulty(preferences, 18, 32, 8, 14, 14, 24))
        {
            return 0;
        }

        if (MatchesDifficulty(preferences, 22, 42, 10, 22, 18, 34))
        {
            return 1;
        }

        if (MatchesDifficulty(preferences, 30, 50, 16, 28, 24, 40))
        {
            return 2;
        }

        return 3;
    }

    private bool MatchesDifficulty(
        LevelGenerationPreferences preferences,
        int minSteps,
        int maxSteps,
        int minPushes,
        int maxPushes,
        int minPulls,
        int maxPulls
    )
    {
        return preferences.minSolutionSteps == minSteps
            && preferences.maxSolutionSteps == maxSteps
            && preferences.minPushes == minPushes
            && preferences.maxPushes == maxPushes
            && preferences.minReversePulls == minPulls
            && preferences.maxReversePulls == maxPulls;
    }

    private int ResolveWaterIndex()
    {
        int min = settings.preferences.minWaterAreas;
        int max = settings.preferences.maxWaterAreas;

        if (min < 0 || max < 0)
        {
            return 0;
        }

        return min == max ? Mathf.Clamp(min + 1, 1, 3) : 0;
    }

    private int ResolveWallIndex()
    {
        int min = settings.preferences.minWallObstacleBlocks;
        int max = settings.preferences.maxWallObstacleBlocks;

        if (min < 0 || max < 0)
        {
            return 0;
        }

        if (min == 2 && max == 3)
        {
            return 4;
        }

        return min == max ? Mathf.Clamp(min + 1, 1, 3) : 0;
    }

    private int ResolveCorridorIndex()
    {
        LevelGenerationPreferences preferences = settings.preferences;

        if (string.IsNullOrEmpty(preferences.corridorPlacement))
        {
            return 0;
        }

        if (preferences.corridorPlacement == "none")
        {
            return 1;
        }

        if (preferences.corridorPlacement == "center")
        {
            return preferences.corridorWidth == 2 ? 3 : 2;
        }

        if (preferences.corridorPlacement == "side")
        {
            return preferences.corridorWidth == 2 ? 5 : 4;
        }

        return 0;
    }


    private void OnNumericRangeEdited()
    {
        ValidateAndUpdateStatus(false);
    }


    private int FindValueIndex(
        string[] values,
        string value,
        int fallback
    )
    {
        if (values == null || string.IsNullOrEmpty(value))
        {
            return fallback;
        }

        for (int i = 0; i < values.Length; i++)
        {
            if (string.Equals(values[i], value, StringComparison.Ordinal))
            {
                return i;
            }
        }

        return fallback;
    }

    private void SetStatus(string message, bool isError)
    {
        if (statusText == null)
        {
            return;
        }

        statusText.text = message;
        statusText.color = isError
            ? new Color(0.72f, 0.12f, 0.08f, 1f)
            : new Color(0.18f, 0.30f, 0.20f, 1f);
    }
}
