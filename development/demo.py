from flowsheet import Stream
from elphick.mc.mass_composition.data.sample_data import sample_data


obj_strm: Stream = Stream.from_dataframe(data=sample_data(), name='my_name')

print(obj_strm.name, '\n', obj_strm._obj)

print('done')