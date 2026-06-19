using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Box : MonoBehaviour
{   
    public Transform start;
    public Transform target;
    public PlayerAnimation anim;

    public void Move(Vector3 dir)
    {
        transform.position += dir;
    }

    public void Awake()
    {
        transform.position = start.position;
    }

    public void Update()
    {
        Reach();
    }

    public void Reach()
    {
        if (transform.position == target.position)
        {
            anim.Animation();
        }
    }
}
