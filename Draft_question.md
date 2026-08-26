# DG Draft Questions

The DG scene asks four neutral level-design questions before generating the Starting Draft.

## Question 1

**How much inspection should the map require before the first move?**

1. Little inspection before acting  
   Internal code: `quick_start`
2. Some inspection of boxes, goals, and passages  
   Internal code: `observe_then_decide`
3. Broader inspection and planning before acting  
   Internal code: `plan_ahead`
4. No preference  
   Internal code: `no_preference`

## Question 2

**How much should box-push decisions depend on other pushes?**

1. Most pushes can be considered independently  
   Internal code: `easy_to_adjust`
2. Some pushes depend on position or order  
   Internal code: `consider_order`
3. Several pushes depend on one another  
   Internal code: `connected_pushes`
4. No preference  
   Internal code: `no_preference`

## Question 3

**How should important positions be distributed across the playable space?**

1. Concentrated within one main area  
   Internal code: `focused_area`
2. Distributed across a few connected areas  
   Internal code: `connected_areas`
3. Distributed across a wider area  
   Internal code: `wide_area`
4. No preference  
   Internal code: `no_preference`

## Question 4

**What route structure would you prefer?**

1. Short routes connecting nearby decisions  
   Internal code: `short_routes`
2. Mostly direct routes with some detours  
   Internal code: `occasional_detours`
3. Longer routes with exploration or return paths  
   Internal code: `long_routes`
4. No preference  
   Internal code: `no_preference`
