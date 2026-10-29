// Hand-written verification example, not AI-generated. All dimensions are mm.
outer_diameter = 24;
inner_diameter = 8;
height = 6;
$fn = 64;
assert(outer_diameter > inner_diameter && inner_diameter > 0 && height > 0);
difference() {
    cylinder(d = outer_diameter, h = height);
    translate([0, 0, -0.1]) cylinder(d = inner_diameter, h = height + 0.2);
}
