;; ADR-034 §0: immutable K2 component; no imports and no host effects.
(component
  (core module $traps
    (memory 1 1)
    (func (export "oob") (param $address i32) (result i32)
      local.get $address
      i32.load)
    (func (export "spin")
      (loop $again br $again))
    (func $recurse (export "recurse") (param $n i32) (result i32)
      local.get $n
      i32.const 1
      i32.add
      call $recurse
      ;; Keep the recursive call non-tail even if tail calls become available.
      i32.const 1
      i32.add))
  (core instance $instance (instantiate $traps))
  (func (export "oob") (param "address" u32) (result u32)
    (canon lift (core func $instance "oob")))
  (func (export "spin") (canon lift (core func $instance "spin")))
  (func (export "recurse") (param "n" u32) (result u32)
    (canon lift (core func $instance "recurse"))))
