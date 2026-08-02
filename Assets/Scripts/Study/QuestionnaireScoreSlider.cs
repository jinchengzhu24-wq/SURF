using UnityEngine;
using UnityEngine.UI;

public class QuestionnaireScoreSlider : MonoBehaviour
{
    public int questionIndex = 1;
    public string questionId = "q1";
    public Text questionText;
    public Slider scoreSlider;
    public Text scoreText;
    [Range(1, 5)]
    public int defaultScore = 3;

    private bool isPrepared;

    public int CurrentScore
    {
        get
        {
            Prepare();
            return scoreSlider != null
                ? Mathf.RoundToInt(scoreSlider.value)
                : Mathf.Clamp(defaultScore, 1, 5);
        }
    }

    public bool HasValidScore
    {
        get
        {
            int score = CurrentScore;
            return scoreSlider != null && score >= 1 && score <= 5;
        }
    }

    public string QuestionTextValue
    {
        get
        {
            return questionText != null ? questionText.text : questionId;
        }
    }

    private void Awake()
    {
        Prepare();
    }

    private void OnDestroy()
    {
        if (scoreSlider != null)
        {
            scoreSlider.onValueChanged.RemoveListener(OnScoreChanged);
        }
    }

    public void Prepare()
    {
        ResolveReferences();

        if (scoreSlider == null)
        {
            return;
        }

        float initialValue = scoreSlider.value;
        scoreSlider.minValue = 1f;
        scoreSlider.maxValue = 5f;
        scoreSlider.wholeNumbers = true;

        if (!isPrepared || initialValue < 1f || initialValue > 5f)
        {
            scoreSlider.SetValueWithoutNotify(
                Mathf.Clamp(defaultScore, 1, 5)
            );
        }
        else
        {
            scoreSlider.SetValueWithoutNotify(Mathf.Round(initialValue));
        }

        scoreSlider.onValueChanged.RemoveListener(OnScoreChanged);
        scoreSlider.onValueChanged.AddListener(OnScoreChanged);
        isPrepared = true;
        UpdateScoreText();
    }

    private void ResolveReferences()
    {
        if (scoreSlider == null)
        {
            scoreSlider = GetComponentInChildren<Slider>(true);
        }

        if (scoreText == null)
        {
            Transform scoreTextTransform = transform.Find("ScoreValueText");

            if (scoreTextTransform != null)
            {
                scoreText = scoreTextTransform.GetComponent<Text>();
            }
        }
    }

    private void OnScoreChanged(float value)
    {
        UpdateScoreText();
    }

    private void UpdateScoreText()
    {
        if (scoreText != null && scoreSlider != null)
        {
            scoreText.text = Mathf.RoundToInt(scoreSlider.value).ToString();
        }
    }
}
